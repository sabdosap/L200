import pytest
from pydantic import ValidationError

from carethread.errors import ErrorCategory, GuidedErrorEnvelope, GuidedErrorException
from carethread.schemas.v1.continuity import (
    ContinuityBriefPayload,
    ContinuitySource,
    CurrentEncounterContext,
    PriorHistoryItem,
)
from carethread.schemas.v1.insurer import CoverageChecklist, InsurerPacketPayload
from carethread.schemas.v1.jobs import JobState
from carethread.schemas.v1.soap import (
    MedicationItem,
    MedicationStatus,
    SoapDraftPayload,
    SoapItem,
    TranscriptSegment,
    compute_canonical_sha256,
)


def test_medication_status_rejects_invalid_enum():
    with pytest.raises(ValidationError):
        MedicationItem(
            item_id="MED-01",
            medication_name="gabapentin",
            dose=300,
            unit="mg",
            route="oral",
            frequency="at bedtime",
            event_date="2026-01-31",
            medication_status="active_assumed",  # invalid
            status_rationale="invalid",
            evidence_ids=["SEG-033"],
        )


def test_missing_statement_requires_missing_reason():
    with pytest.raises(ValidationError):
        SoapDraftPayload.model_validate({"schema_version": "1.0.0"})


def test_soap_item_null_statement_requires_missing_reason():
    with pytest.raises(ValidationError):
        SoapItem(
            item_id="OBJ-02",
            category="vital_signs",
            statement=None,
            evidence_ids=[],
        )

    valid_missing = SoapItem(
        item_id="OBJ-02",
        category="vital_signs",
        statement=None,
        missing_reason="NOT_STATED_IN_TRANSCRIPT",
        evidence_ids=[],
    )
    assert valid_missing.statement is None
    assert valid_missing.missing_reason == "NOT_STATED_IN_TRANSCRIPT"


def test_soap_item_non_null_statement_requires_evidence_ids():
    with pytest.raises(ValidationError):
        SoapItem(
            item_id="SUBJ-01",
            category="chief_complaint",
            statement="Lower back pain for 6 weeks.",
            evidence_ids=[],
        )


def test_medication_status_has_exact_six_values():
    assert {s.value for s in MedicationStatus} == {
        "discussed",
        "planned",
        "prescribed",
        "reported",
        "stopped",
        "unclear",
    }


def test_medication_item_null_field_requires_missing_reason():
    with pytest.raises(ValidationError):
        MedicationItem(
            item_id="MED-03",
            medication_name="gabapentin",
            dose=None,
            unit="mg",
            route="oral",
            frequency="at bedtime",
            event_date="2026-01-31",
            medication_status=MedicationStatus.DISCUSSED,
            status_rationale="Dose not specified during discussion.",
            evidence_ids=["SEG-033"],
        )

    item = MedicationItem(
        item_id="MED-03",
        medication_name="gabapentin",
        dose=None,
        unit="mg",
        route="oral",
        frequency="at bedtime",
        missing_reason="NOT_STATED_IN_TRANSCRIPT",
        event_date="2026-01-31",
        medication_status=MedicationStatus.DISCUSSED,
        status_rationale="Dose not specified during discussion.",
        evidence_ids=["SEG-033"],
    )
    assert item.dose is None
    assert item.missing_reason == "NOT_STATED_IN_TRANSCRIPT"


def test_canonical_sha256_is_deterministic():
    c1 = CoverageChecklist(numerator=4, denominator=5, missing_items=["MRI lumbar spine report"])
    c2 = CoverageChecklist(numerator=4, denominator=5, missing_items=["MRI lumbar spine report"])
    assert compute_canonical_sha256(c1) == compute_canonical_sha256(c2)
    assert len(compute_canonical_sha256(c1)) == 64


def test_coverage_checklist_rejects_invalid_bounds():
    with pytest.raises(ValidationError):
        CoverageChecklist(numerator=6, denominator=5, missing_items=[])
    with pytest.raises(ValidationError):
        CoverageChecklist(numerator=-1, denominator=5, missing_items=[])


def test_guided_error_envelope_forbids_extra_fields():
    err = GuidedErrorEnvelope(
        error_code="EVIDENCE_SPAN_NOT_FOUND",
        category=ErrorCategory.VALIDATION_RECOVERABLE,
        retryable=True,
        max_retries_remaining=1,
        field_path="soap.plan.medications[0].evidence_ids[0]",
        guided_recovery="Re-read segments via fetch_verbatim_transcript_segments.",
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
    )
    assert err.schema_version == "1.0.0"
    assert err.status == "ERROR"

    with pytest.raises(ValidationError):
        GuidedErrorEnvelope(
            error_code="EVIDENCE_SPAN_NOT_FOUND",
            category=ErrorCategory.VALIDATION_RECOVERABLE,
            retryable=True,
            max_retries_remaining=1,
            field_path="soap.plan.medications[0].evidence_ids[0]",
            guided_recovery="Re-read segments.",
            trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
            unexpected_field="nope",
        )


def test_guided_error_exception_wraps_envelope():
    err = GuidedErrorEnvelope(
        error_code="TENANT_MISMATCH",
        category=ErrorCategory.AUTHZ_DENIED,
        retryable=False,
        max_retries_remaining=0,
        field_path="tenant_id",
        guided_recovery="Abort request; tenant mismatch.",
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
    )
    exc = GuidedErrorException(err)
    assert exc.envelope is err
    assert "AUTHZ_DENIED" in str(exc)


def test_continuity_brief_enforces_cross_visit_separation():
    with pytest.raises(ValidationError):
        ContinuityBriefPayload(
            patient_id="PT-SYN-0001",
            continuity_source=ContinuitySource.SPANNER_GRAPH_VERIFIED,
            prior_history_items=[
                PriorHistoryItem(
                    item_id="HIST-01",
                    encounter_id="ENC-2026-0331-ORTHO",
                    encounter_date="2026-03-31",
                    specialty="Orthopedic Surgery",
                    category="medication",
                    summary="Discussed gabapentin",
                    evidence_ids=["SEG-033"],
                )
            ],
            current_encounter=CurrentEncounterContext(
                encounter_id="ENC-2026-0331-ORTHO",
                encounter_date="2026-03-31",
                specialty="Orthopedic Surgery",
            ),
        )


def test_insurer_packet_rejects_out_of_scope_purpose():
    with pytest.raises(ValidationError):
        InsurerPacketPayload(
            packet_id="PKT-001",
            packet_version=1,
            grant_id="GRT-001",
            patient_id="PT-SYN-0001",
            consent_version_id="CNS-001",
            purpose="underwriting_risk_score",
            coverage_checklist=CoverageChecklist(numerator=4, denominator=5, missing_items=["MRI"]),
        )


def test_job_state_contains_all_spec_states():
    expected = {
        "QUEUED",
        "TRANSCRIBING",
        "FAILED_RETRYABLE",
        "TRANSCRIBED",
        "DRAFTING",
        "AWAITING_CLINICIAN_REVIEW",
        "REJECTED_BY_CLINICIAN",
        "COMMITTING_FHIR",
        "RECONCILING_FHIR",
        "SIGNED_COMMITTED",
        "FAILED_TERMINAL",
    }
    assert {s.value for s in JobState} == expected


def test_transcript_segment_validates_timestamps():
    seg = TranscriptSegment(
        evidence_id="SEG-001",
        speaker_label="SPEAKER_1",
        start_ms=100,
        end_ms=500,
        text="What brings you in today?",
    )
    assert seg.end_ms == 500

    with pytest.raises(ValidationError):
        TranscriptSegment(
            evidence_id="SEG-001",
            speaker_label="SPEAKER_1",
            start_ms=500,
            end_ms=100,
            text="Invalid timestamps",
        )


def test_full_spec_soap_draft_payload_validates_and_hashes_deterministically():
    sample = {
        "schema_version": "1.0.0",
        "draft_id": "DRF-2026-0131-0001",
        "draft_version": 1,
        "status": "DRAFT_AWAITING_CLINICIAN_REVIEW",
        "encounter_ref": {
            "encounter_id": "ENC-2026-0131-NEURO",
            "encounter_date": "2026-01-31",
            "specialty": "Neurology",
        },
        "source_refs": {
            "transcript_version_id": "TR-2026-0131-v1",
            "fhir_prior_refs": [],
        },
        "speaker_attribution": {
            "status": "UNVERIFIED_REQUIRES_CLINICIAN_CONFIRMATION",
            "candidates": [
                {"diarization_label": "SPEAKER_1", "proposed_role": "CLINICIAN"},
                {"diarization_label": "SPEAKER_2", "proposed_role": "PATIENT"},
            ],
        },
        "soap": {
            "subjective": [
                {
                    "item_id": "SUBJ-01",
                    "category": "chief_complaint",
                    "statement": "Lower back pain for 6 weeks radiating to the left posterior thigh.",
                    "polarity": "PRESENT",
                    "speaker_role": "PATIENT",
                    "evidence_ids": ["SEG-004", "SEG-005"],
                }
            ],
            "objective": [
                {
                    "item_id": "OBJ-01",
                    "category": "stated_exam_finding",
                    "statement": "Clinician states left straight-leg raise positive at 45 degrees.",
                    "status": "STATED_IN_AUDIO",
                    "evidence_ids": ["SEG-021"],
                },
                {
                    "item_id": "OBJ-02",
                    "category": "vital_signs",
                    "statement": None,
                    "missing_reason": "NOT_STATED_IN_TRANSCRIPT",
                    "evidence_ids": [],
                },
            ],
            "assessment": [
                {
                    "item_id": "ASSESS-01",
                    "statement": "Suspected left L5-S1 lumbar radiculopathy.",
                    "certainty": "UNCERTAIN_WORKING_IMPRESSION",
                    "evidence_ids": ["SEG-028"],
                }
            ],
            "plan": {
                "medications": [
                    {
                        "item_id": "MED-01",
                        "medication_name": "gabapentin",
                        "dose": 300,
                        "unit": "mg",
                        "route": "oral",
                        "frequency": "at bedtime",
                        "event_date": "2026-01-31",
                        "medication_status": "discussed",
                        "status_rationale": "Discussed as option if pain persists after MRI.",
                        "evidence_ids": ["SEG-033", "SEG-034"],
                    }
                ],
                "lifestyle_and_activity": [
                    {
                        "item_id": "LIFE-01",
                        "category": "physical_activity",
                        "statement": "Advised gentle daily walking as tolerated.",
                        "evidence_ids": ["SEG-038"],
                    }
                ],
                "follow_up": [
                    {
                        "item_id": "FU-01",
                        "statement": "Lumbar spine MRI ordered; follow-up in 6 to 8 weeks.",
                        "evidence_ids": ["SEG-041"],
                    }
                ],
            },
        },
        "review_flags": [
            {
                "flag_id": "FLG-01",
                "severity": "REQUIRED_ACTION",
                "code": "VERIFY_SPEAKER_ROLES",
                "message": "Confirm SPEAKER_1=Clinician and SPEAKER_2=Patient before signing.",
            }
        ],
    }

    payload = SoapDraftPayload.model_validate(sample)
    digest1 = compute_canonical_sha256(payload)
    digest2 = compute_canonical_sha256(SoapDraftPayload.model_validate(sample))
    assert digest1 == digest2
    assert len(digest1) == 64
