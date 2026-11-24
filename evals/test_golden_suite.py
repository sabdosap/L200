"""10-Case Hard-Gated Golden Evaluation Suite (GC-01 through GC-10) + Static Hygiene Checks.

Every golden test exercises the real CareThread runtime modules end-to-end using 100%
fabricated synthetic fixtures (PT-SYN-0001 "Alex Rivera", ENC-2026-0131-NEURO Dr. Maya Lin,
ENC-2026-0331-ORTHO Dr. David Okafor, and "ClearPath Health Plan").
"""

from __future__ import annotations

import ast
from datetime import timedelta
import json
from pathlib import Path
import re
from typing import Any

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import pytest

from carethread.errors import ErrorCategory, GuidedErrorException
from carethread.hitl.confirmations import ConfirmationStatus
from carethread.observability.tracing import TracingContext
from carethread.schemas.v1.continuity import ContinuitySource
from carethread.schemas.v1.jobs import JobState
from carethread.schemas.v1.soap import MedicationStatus
from carethread.services.insurer_release import ChecklistRequirement
from carethread.stores.outbox import GRAPH_REFRESH_PENDING
from carethread.tools.clinical_tools import EXPECTED_ADK_TOOL_NAMES
from carethread.tools.registry import REQUIRED_DOCSTRING_SECTIONS, validate_tool_definition
from evals.fixtures.synthetic_cases import (
    DR_MAYA_LIN_ID,
    DR_MAYA_LIN_NAME,
    INSURER_ID,
    JAN_31_DATE,
    JAN_31_ENCOUNTER_ID,
    JAN_31_NOW,
    MAR_31_DATE,
    MAR_31_ENCOUNTER_ID,
    MAR_31_NOW,
    PATIENT_DISPLAY_NAME,
    PATIENT_ID,
    PATIENT_SYNTHETIC_DOB,
    PATIENT_SYNTHETIC_MRN,
    SYNTHETIC_RAW_AUDIO_PATH,
    TENANT_A_ID,
    build_active_consent,
    build_eval_runtime,
    build_jan31_neurology_soap_draft,
    build_mar31_orthopedics_context,
    build_revoked_consent,
    build_synthetic_actors,
    seed_base_fhir_entities,
    seed_jan31_signed_fhir_and_graph,
    store_jan31_transcript,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
CARETHREAD_SRC_ROOT = SRC_ROOT / "carethread"


# ---------------------------------------------------------------------------
# GC-01: Two-Visit Handoff (Jan 31 Neurology -> Mar 31 Orthopedics)
# ---------------------------------------------------------------------------


def test_gc01_two_visit_handoff() -> None:
    """GC-01 Hard Gate: Jan 31 Neurology facts surface in Mar 31 Orthopedics with date, attribution, and separation."""
    runtime = build_eval_runtime()
    commit_result = seed_jan31_signed_fhir_and_graph(runtime)
    actors = build_synthetic_actors()
    consent = build_active_consent()

    brief = runtime.coordinator.run_continuity_brief_workflow(
        actor=actors["dr_david_okafor"],
        patient_id=PATIENT_ID,
        patient_tenant_id=TENANT_A_ID,
        consent_record=consent,
        current_encounter=build_mar31_orthopedics_context(),
        as_of_date=MAR_31_DATE,
        session_id="sess-gc01-ortho",
        now=MAR_31_NOW,
    )

    assert brief.patient_id == PATIENT_ID
    assert brief.continuity_source == ContinuitySource.SPANNER_GRAPH_VERIFIED
    assert brief.staleness_banner is None
    assert brief.current_encounter.encounter_id == MAR_31_ENCOUNTER_ID
    assert brief.current_encounter.specialty == "Orthopedics"
    assert brief.prior_history_items, "Expected Jan 31 Neurology prior history items"

    # Every prior item must carry Jan 31 date, Neurology attribution, Dr. Maya Lin, and evidence/FHIR refs.
    for item in brief.prior_history_items:
        assert item.encounter_id == JAN_31_ENCOUNTER_ID
        assert item.encounter_id != MAR_31_ENCOUNTER_ID
        assert item.encounter_date == JAN_31_DATE
        assert item.specialty == "Neurology"
        assert item.practitioner_name == DR_MAYA_LIN_NAME
        assert item.evidence_ids
        assert item.fhir_ref is not None

    # Verify Jan 31 signed Provenance exists in authoritative FHIR targeting the signed Composition.
    provenances = runtime.fhir_store.list_resources(TENANT_A_ID, resource_type="Provenance")
    assert len(provenances) == 1
    target_refs = {t["reference"] for t in provenances[0].get("target", [])}
    assert commit_result.composition_ref in target_refs

    # Verify key clinical facts from Jan 31 are represented.
    summaries_lower = " ".join(item.summary.lower() for item in brief.prior_history_items)
    assert "radiculopathy" in summaries_lower
    assert "gabapentin" in summaries_lower
    assert "ibuprofen" in summaries_lower

    # Verify signed Jan 31 Observations (SLR exam, back pain, walking/diet, MRI follow-up) exist in FHIR.
    observations = runtime.fhir_store.list_resources(TENANT_A_ID, resource_type="Observation")
    obs_text = " ".join(str(o.get("valueString", "")).lower() for o in observations)
    assert "straight leg raise" in obs_text
    assert "lower back pain" in obs_text
    compositions = runtime.fhir_store.list_resources(TENANT_A_ID, resource_type="Composition")
    assert len(compositions) == 1
    comp_plan_div = " ".join(
        str((sec.get("text") or {}).get("div", "")).lower()
        for sec in compositions[0].get("section", [])
    )
    assert "walking" in comp_plan_div
    assert "mri" in comp_plan_div


# ---------------------------------------------------------------------------
# GC-02: Medication Ambiguity & Negation
# ---------------------------------------------------------------------------


def test_gc02_medication_ambiguity_and_negation() -> None:
    """GC-02 Hard Gate: Gabapentin stays 'discussed' (zero MedicationRequest), ibuprofen 'stopped', red flags NEGATED."""
    runtime = build_eval_runtime()
    seed_base_fhir_entities(runtime.fhir_store, tenant_id=TENANT_A_ID)
    object_path, version_id = store_jan31_transcript(runtime.artifact_store, tenant_id=TENANT_A_ID)

    # Attempt 0 wrongly upgrades gabapentin -> PRESCRIBED; deterministic EvidenceVerifier must reject it,
    # forcing bounded retry Attempt 1 to classify gabapentin as DISCUSSED.
    def _extractor(_prompt: str, _segments: list[Any], _route: Any, feedback: Any, attempt: int) -> Any:
        if attempt == 0:
            return build_jan31_neurology_soap_draft(
                transcript_version_id=version_id,
                gabapentin_status=MedicationStatus.PRESCRIBED,
            )
        assert feedback is not None
        assert feedback.error_code == "MEDICATION_STATUS_UPGRADE_VIOLATION"
        return build_jan31_neurology_soap_draft(
            transcript_version_id=version_id,
            gabapentin_status=MedicationStatus.DISCUSSED,
        )

    pipeline_result = runtime.coordinator.run_visit_draft_pipeline(
        tenant_id=TENANT_A_ID,
        encounter_id=JAN_31_ENCOUNTER_ID,
        transcript_object_path=object_path,
        session_id="sess-gc02-neuro",
        user_id=DR_MAYA_LIN_ID,
        soap_extractor=_extractor,
        now=JAN_31_NOW,
    )

    assert pipeline_result.status == JobState.AWAITING_CLINICIAN_REVIEW
    assert pipeline_result.regeneration_count == 1
    assert pipeline_result.draft is not None
    assert pipeline_result.confirmation is not None
    assert pipeline_result.draft_sha256 is not None

    meds_by_name = {m.medication_name.lower(): m for m in pipeline_result.draft.soap.plan.medications}
    assert meds_by_name["gabapentin"].medication_status == MedicationStatus.DISCUSSED
    assert meds_by_name["ibuprofen"].medication_status == MedicationStatus.STOPPED

    negated_assessments = [
        item for item in pipeline_result.draft.soap.assessment if item.polarity == "NEGATED"
    ]
    assert len(negated_assessments) == 1
    assert "bowel or bladder" in (negated_assessments[0].statement or "").lower()

    # Approve HITL confirmation and commit signed SOAP to authoritative FHIR R4.
    runtime.hitl_store.approve(
        confirmation_id=pipeline_result.confirmation.confirmation_id,
        actor_id=DR_MAYA_LIN_ID,
        expected_version=pipeline_result.draft.draft_version,
        expected_digest=pipeline_result.draft_sha256,
        now=JAN_31_NOW,
    )
    runtime.fhir_writer.commit_signed_soap(
        tenant_id=TENANT_A_ID,
        encounter_id=JAN_31_ENCOUNTER_ID,
        signed_soap=pipeline_result.draft,
        signer_id=DR_MAYA_LIN_ID,
        transcript_ref=f"gs://carethread-private-artifacts/{object_path}#{version_id}",
        idempotency_key="commit-gc02-v1",
    )

    # Zero MedicationRequest resources may be created for discussed/stopped medications.
    med_requests = runtime.fhir_store.list_resources(TENANT_A_ID, resource_type="MedicationRequest")
    assert med_requests == []

    med_statements = runtime.fhir_store.list_resources(
        TENANT_A_ID, resource_type="MedicationStatement"
    )
    stmt_by_name = {
        str((ms.get("medicationCodeableConcept") or {}).get("text", "")).lower(): ms
        for ms in med_statements
    }
    assert stmt_by_name["gabapentin"]["status"] == "not-taken"
    assert stmt_by_name["ibuprofen"]["status"] == "stopped"

    # Rebuild Spanner Graph and verify Mar 31 Orthopedics brief preserves discussed/stopped/NEGATED.
    runtime.graph_store.rebuild_from_fhir(TENANT_A_ID, runtime.fhir_store)
    actors = build_synthetic_actors()
    brief = runtime.coordinator.run_continuity_brief_workflow(
        actor=actors["dr_david_okafor"],
        patient_id=PATIENT_ID,
        patient_tenant_id=TENANT_A_ID,
        consent_record=build_active_consent(),
        current_encounter=build_mar31_orthopedics_context(),
        as_of_date=MAR_31_DATE,
        session_id="sess-gc02-ortho",
        now=MAR_31_NOW,
    )

    gabapentin_items = [
        item
        for item in brief.prior_history_items
        if item.category == "medication" and "gabapentin" in item.summary.lower()
    ]
    ibuprofen_items = [
        item
        for item in brief.prior_history_items
        if item.category == "medication" and "ibuprofen" in item.summary.lower()
    ]
    negated_probs = [
        item
        for item in brief.prior_history_items
        if item.category == "problem" and item.polarity == "NEGATED"
    ]
    assert len(gabapentin_items) == 1
    assert gabapentin_items[0].medication_status == MedicationStatus.DISCUSSED
    assert "discussed" in gabapentin_items[0].summary.lower()
    assert len(ibuprofen_items) == 1
    assert ibuprofen_items[0].medication_status == MedicationStatus.STOPPED
    assert len(negated_probs) == 1
    assert "bowel or bladder" in negated_probs[0].summary.lower()


# ---------------------------------------------------------------------------
# GC-03: Failed Authorization
# ---------------------------------------------------------------------------


def test_gc03_failed_authorization() -> None:
    """GC-03 Hard Gate: Cross-tenant and no-care-relationship requests fail closed before FHIR/Spanner/LLM."""
    runtime = build_eval_runtime()
    seed_jan31_signed_fhir_and_graph(runtime)
    actors = build_synthetic_actors()
    consent = build_active_consent()

    # Track calls to prove zero Spanner Graph or FHIR reads occur on unauthorized access.
    graph_calls = 0
    fhir_read_calls = 0
    orig_graph_query = runtime.graph_store.query_patient_continuity
    orig_fhir_read = runtime.fhir_store.read

    def _counting_graph_query(*args: Any, **kwargs: Any) -> Any:
        nonlocal graph_calls
        graph_calls += 1
        return orig_graph_query(*args, **kwargs)

    def _counting_fhir_read(*args: Any, **kwargs: Any) -> Any:
        nonlocal fhir_read_calls
        fhir_read_calls += 1
        return orig_fhir_read(*args, **kwargs)

    runtime.graph_store.query_patient_continuity = _counting_graph_query  # type: ignore[method-assign]
    runtime.fhir_store.read = _counting_fhir_read  # type: ignore[method-assign]

    # 1. Tenant B clinician requesting Tenant A's PT-SYN-0001 continuity history.
    with pytest.raises(GuidedErrorException) as exc_tenant_b:
        runtime.coordinator.run_continuity_brief_workflow(
            actor=actors["tenant_b_clinician"],
            patient_id=PATIENT_ID,
            patient_tenant_id=TENANT_A_ID,
            consent_record=consent,
            current_encounter=build_mar31_orthopedics_context(),
            as_of_date=MAR_31_DATE,
            session_id="sess-gc03-tenantb",
            now=MAR_31_NOW,
        )
    assert exc_tenant_b.value.envelope.category == ErrorCategory.AUTHZ_DENIED
    assert exc_tenant_b.value.envelope.error_code == "TENANT_ISOLATION_VIOLATION"

    # 2. Tenant A clinician without legitimate care relationship to PT-SYN-0001.
    with pytest.raises(GuidedErrorException) as exc_no_rel:
        runtime.coordinator.run_continuity_brief_workflow(
            actor=actors["unrelated_tenant_a_clinician"],
            patient_id=PATIENT_ID,
            patient_tenant_id=TENANT_A_ID,
            consent_record=consent,
            current_encounter=build_mar31_orthopedics_context(),
            as_of_date=MAR_31_DATE,
            session_id="sess-gc03-norel",
            now=MAR_31_NOW,
        )
    assert exc_no_rel.value.envelope.category == ErrorCategory.AUTHZ_DENIED
    assert exc_no_rel.value.envelope.error_code == "CARE_RELATIONSHIP_REQUIRED"

    # 3. Policy plugin blocks unauthorized caller before tool execution and emits paired INTENT + DENIED OUTCOME.
    audit_start_len = len(runtime.audit_sink)
    with pytest.raises(GuidedErrorException) as exc_plugin:
        runtime.tool_registry.invoke_with_policy(
            policy_plugin=runtime.policy_plugin,
            caller_id="soap_extractor_agent",
            tool_name="query_patient_continuity_graph",
            tool_kwargs={},
            tenant_id=TENANT_A_ID,
            workflow="continuity_brief_workflow",
            job_id="job-gc03-denied",
            trace_id="trace-gc03-denied",
            now=MAR_31_NOW,
        )
    assert exc_plugin.value.envelope.category == ErrorCategory.AUTHZ_DENIED
    assert exc_plugin.value.envelope.error_code == "CALLER_NOT_ALLOWLISTED"

    # Zero Spanner or FHIR calls were executed.
    assert graph_calls == 0
    assert fhir_read_calls == 0

    # Verify paired ACTION_INTENT and ACTION_OUTCOME(DENIED) with zero clinical data.
    new_logs = runtime.audit_sink[audit_start_len:]
    intents = [log for log in new_logs if log["event_type"] == "ACTION_INTENT"]
    outcomes = [log for log in new_logs if log["event_type"] == "ACTION_OUTCOME"]
    assert len(intents) == 1
    assert len(outcomes) == 1
    assert intents[0]["action_id"] == outcomes[0]["action_id"]
    assert outcomes[0]["outcome"] == "DENIED"
    serialized_logs = json.dumps(new_logs)
    assert PATIENT_DISPLAY_NAME not in serialized_logs
    assert "radiculopathy" not in serialized_logs.lower()
    assert "gabapentin" not in serialized_logs.lower()


# ---------------------------------------------------------------------------
# GC-04: Stale Graph -> Authoritative FHIR R4 Fallback
# ---------------------------------------------------------------------------


def test_gc04_stale_graph_authoritative_fallback() -> None:
    """GC-04 Hard Gate: Version mismatch or graph timeout triggers authoritative FHIR R4 fallback with banner."""
    runtime = build_eval_runtime()
    seed_jan31_signed_fhir_and_graph(runtime)
    actors = build_synthetic_actors()
    consent = build_active_consent()

    # Amend Jan 31 Composition in authoritative FHIR R4 from versionId=1 -> versionId=2,
    # while Spanner Graph still holds stale versionId=1.
    compositions = runtime.fhir_store.list_resources(TENANT_A_ID, resource_type="Composition")
    assert len(compositions) == 1
    comp = compositions[0]
    assert comp["meta"]["versionId"] == "1"
    comp["title"] = f"Signed SOAP Note - {JAN_31_ENCOUNTER_ID} (Amended v2)"
    updated_comp = runtime.fhir_store.update(TENANT_A_ID, comp, if_match='W/"1"')
    assert updated_comp["meta"]["versionId"] == "2"

    # Part A: Stale versionId mismatch -> verify_fhir_resource_versions triggers authoritative fallback.
    brief_stale = runtime.coordinator.run_continuity_brief_workflow(
        actor=actors["dr_david_okafor"],
        patient_id=PATIENT_ID,
        patient_tenant_id=TENANT_A_ID,
        consent_record=consent,
        current_encounter=build_mar31_orthopedics_context(),
        as_of_date=MAR_31_DATE,
        session_id="sess-gc04-stale",
        now=MAR_31_NOW,
    )
    assert brief_stale.continuity_source == ContinuitySource.FHIR_R4_AUTHORITATIVE
    assert brief_stale.staleness_banner is not None
    assert "authoritative fhir r4" in brief_stale.staleness_banner.lower()

    comp_refs = [ref for ref in brief_stale.fhir_version_refs if ref.resource_type == "Composition"]
    assert len(comp_refs) == 1
    assert comp_refs[0].version_id == "2"

    comp_items = [item for item in brief_stale.prior_history_items if item.category == "clinical_note"]
    assert len(comp_items) == 1
    assert "Amended v2" in comp_items[0].summary

    # Part B: Simulated Spanner Graph timeout also falls back cleanly to authoritative FHIR R4 v2.
    runtime.graph_store.simulated_latency_ms = 5000
    brief_timeout = runtime.coordinator.run_continuity_brief_workflow(
        actor=actors["dr_david_okafor"],
        patient_id=PATIENT_ID,
        patient_tenant_id=TENANT_A_ID,
        consent_record=consent,
        current_encounter=build_mar31_orthopedics_context(),
        as_of_date=MAR_31_DATE,
        session_id="sess-gc04-timeout",
        now=MAR_31_NOW,
    )
    assert brief_timeout.continuity_source == ContinuitySource.FHIR_R4_AUTHORITATIVE
    assert brief_timeout.staleness_banner is not None
    assert "timed out" in brief_timeout.staleness_banner.lower()
    timeout_comp_refs = [
        ref for ref in brief_timeout.fhir_version_refs if ref.resource_type == "Composition"
    ]
    assert len(timeout_comp_refs) == 1
    assert timeout_comp_refs[0].version_id == "2"


# ---------------------------------------------------------------------------
# GC-05: Changed Consent Invalidates Access
# ---------------------------------------------------------------------------


def test_gc05_changed_consent_invalidates_access() -> None:
    """GC-05 Hard Gate: Consent revocation or version supersession immediately blocks retrieval and insurer release."""
    runtime = build_eval_runtime()
    seed_jan31_signed_fhir_and_graph(runtime)
    actors = build_synthetic_actors()
    active_consent = build_active_consent()

    # Grant created while consent v1 is active.
    grant = runtime.grant_service.create_grant(
        tenant_id=TENANT_A_ID,
        patient_id=PATIENT_ID,
        insurer_id=INSURER_ID,
        purpose="prior_auth_documentation",
        consent_record=active_consent,
        now=MAR_31_NOW,
    )

    # Preview succeeds under active consent v1.
    preview = runtime.coordinator.run_insurer_packet_workflow(
        grant=grant,
        consent_record=active_consent,
        checklist_requirements=["Neurology consultation note"],
        session_id="sess-gc05-preview-ok",
        now=MAR_31_NOW,
    )
    assert preview.status == "AWAITING_HUMAN_RELEASE"

    # Patient revokes consent before release / Mar 31 retrieval.
    revoked_consent = build_revoked_consent()

    # 1. Mar 31 continuity retrieval must fail closed with CONSENT_REVOKED.
    with pytest.raises(GuidedErrorException) as exc_cont:
        runtime.coordinator.run_continuity_brief_workflow(
            actor=actors["dr_david_okafor"],
            patient_id=PATIENT_ID,
            patient_tenant_id=TENANT_A_ID,
            consent_record=revoked_consent,
            current_encounter=build_mar31_orthopedics_context(),
            as_of_date=MAR_31_DATE,
            session_id="sess-gc05-cont-revoked",
            now=MAR_31_NOW,
        )
    assert exc_cont.value.envelope.category == ErrorCategory.CONSENT_REVOKED
    assert exc_cont.value.envelope.error_code == "PATIENT_CONSENT_REVOKED"

    # 2. Insurer packet workflow re-verifies active consent and fails closed with CONSENT_REVOKED.
    with pytest.raises(GuidedErrorException) as exc_rel:
        runtime.coordinator.run_insurer_packet_workflow(
            grant=grant,
            consent_record=revoked_consent,
            checklist_requirements=["Neurology consultation note"],
            session_id="sess-gc05-rel-revoked",
            now=MAR_31_NOW,
        )
    assert exc_rel.value.envelope.category == ErrorCategory.CONSENT_REVOKED
    assert exc_rel.value.envelope.error_code == "PATIENT_CONSENT_REVOKED"

    # 3. Even a new active consent_version_id invalidates the prior grant pinned to v1.
    superseded_consent = build_active_consent(
        consent_version_id="CONSENT-2026-0331-V2",
        updated_at=MAR_31_NOW,
    )
    with pytest.raises(GuidedErrorException) as exc_sup:
        runtime.coordinator.run_insurer_packet_workflow(
            grant=grant,
            consent_record=superseded_consent,
            checklist_requirements=["Neurology consultation note"],
            session_id="sess-gc05-rel-superseded",
            now=MAR_31_NOW,
        )
    assert exc_sup.value.envelope.category == ErrorCategory.CONSENT_REVOKED
    assert exc_sup.value.envelope.error_code == "CONSENT_VERSION_SUPERSEDED"


# ---------------------------------------------------------------------------
# GC-06: Duplicate Delivery & Lost-Response Reconciliation
# ---------------------------------------------------------------------------


def test_gc06_duplicate_delivery_and_lost_response_reconciliation() -> None:
    """GC-06 Hard Gate: 3x duplicate commit + lost-response timeout + 3x graph refresh yields zero duplicates."""
    runtime = build_eval_runtime()
    seed_base_fhir_entities(runtime.fhir_store, tenant_id=TENANT_A_ID)
    object_path, version_id = store_jan31_transcript(runtime.artifact_store, tenant_id=TENANT_A_ID)
    draft = build_jan31_neurology_soap_draft(transcript_version_id=version_id)
    transcript_ref = f"gs://carethread-private-artifacts/{object_path}#{version_id}"

    idempotency_key = "commit-ENC-2026-0131-NEURO-gc06"
    job, created = runtime.outbox_store.create_or_get_job(
        tenant_id=TENANT_A_ID,
        encounter_id=JAN_31_ENCOUNTER_ID,
        idempotency_key=idempotency_key,
        initial_state=JobState.AWAITING_CLINICIAN_REVIEW,
        now=JAN_31_NOW,
    )
    assert created is True

    # Delivery 1: Simulate lost response AFTER FHIR transaction commits.
    runtime.fhir_store.simulate_lost_response_once = True
    with pytest.raises(TimeoutError):
        runtime.reconciler.commit_signed_soap_job(
            job_id=job.job_id,
            worker_id="worker-1",
            signed_soap=draft,
            signer_id=DR_MAYA_LIN_ID,
            transcript_ref=transcript_ref,
            now=JAN_31_NOW,
        )
    assert runtime.outbox_store.get_job(job.job_id).state == JobState.RECONCILING_FHIR

    # Delivery 2: Duplicate Cloud Tasks delivery reconciles existing FHIR commit without creating duplicates.
    res_2 = runtime.reconciler.commit_signed_soap_job(
        job_id=job.job_id,
        worker_id="worker-1",
        signed_soap=draft,
        signer_id=DR_MAYA_LIN_ID,
        transcript_ref=transcript_ref,
        now=JAN_31_NOW + timedelta(seconds=5),
    )
    assert res_2.created_new is False
    assert runtime.outbox_store.get_job(job.job_id).state == JobState.SIGNED_COMMITTED

    # Delivery 3: Third duplicate Cloud Tasks delivery absorbs cleanly and returns identical refs.
    res_3 = runtime.reconciler.commit_signed_soap_job(
        job_id=job.job_id,
        worker_id="worker-1",
        signed_soap=draft,
        signer_id=DR_MAYA_LIN_ID,
        transcript_ref=transcript_ref,
        now=JAN_31_NOW + timedelta(seconds=10),
    )
    assert res_3.created_new is False
    assert res_3.composition_ref == res_2.composition_ref
    assert res_3.provenance_ref == res_2.provenance_ref

    # Exactly 1 signed Composition, 1 Provenance, and 1 GRAPH_REFRESH_PENDING outbox event.
    assert len(runtime.fhir_store.list_resources(TENANT_A_ID, resource_type="Composition")) == 1
    assert len(runtime.fhir_store.list_resources(TENANT_A_ID, resource_type="Provenance")) == 1
    outbox_events = runtime.outbox_store.list_outbox_events(
        tenant_id=TENANT_A_ID,
        event_type=GRAPH_REFRESH_PENDING,
    )
    assert len(outbox_events) == 1

    # Deliver refresh_patient_continuity_graph 3 times -> zero duplicate graph nodes/edges.
    for _ in range(3):
        runtime.graph_store.rebuild_from_fhir(TENANT_A_ID, runtime.fhir_store)

    snapshot = runtime.graph_store.query_patient_continuity(
        tenant_id=TENANT_A_ID,
        patient_id=PATIENT_ID,
        as_of_date=JAN_31_DATE,
    )
    assert len(snapshot.encounters) == 1
    assert len(snapshot.medication_events) == 2
    assert len(snapshot.problem_events) == 2


# ---------------------------------------------------------------------------
# GC-07: Clinician Rejection Blocks FHIR Commit
# ---------------------------------------------------------------------------


def test_gc07_clinician_rejection_blocks_fhir_commit() -> None:
    """GC-07 Hard Gate: Clinician rejection blocks FHIR write, prevents digest replay, and allows v2 regeneration."""
    runtime = build_eval_runtime()
    seed_base_fhir_entities(runtime.fhir_store, tenant_id=TENANT_A_ID)
    object_path, version_id = store_jan31_transcript(runtime.artifact_store, tenant_id=TENANT_A_ID)

    def _extractor_v1(*_args: Any) -> Any:
        return build_jan31_neurology_soap_draft(
            transcript_version_id=version_id,
            draft_version=1,
        )

    res_v1 = runtime.coordinator.run_visit_draft_pipeline(
        tenant_id=TENANT_A_ID,
        encounter_id=JAN_31_ENCOUNTER_ID,
        transcript_object_path=object_path,
        session_id="sess-gc07",
        user_id=DR_MAYA_LIN_ID,
        soap_extractor=_extractor_v1,
        now=JAN_31_NOW,
    )
    assert res_v1.status == JobState.AWAITING_CLINICIAN_REVIEW
    assert res_v1.confirmation is not None
    assert res_v1.draft is not None
    assert res_v1.draft_sha256 is not None

    # Create job tracking the review and transition to REJECTED_BY_CLINICIAN upon Dr. Maya Lin's rejection.
    job, _ = runtime.outbox_store.create_or_get_job(
        tenant_id=TENANT_A_ID,
        encounter_id=JAN_31_ENCOUNTER_ID,
        idempotency_key="commit-gc07-v1",
        initial_state=JobState.AWAITING_CLINICIAN_REVIEW,
        now=JAN_31_NOW,
    )
    rejected = runtime.hitl_store.reject(
        confirmation_id=res_v1.confirmation.confirmation_id,
        actor_id=DR_MAYA_LIN_ID,
        reason="Disputed speaker attribution on SEG-003; regenerate draft.",
        now=JAN_31_NOW + timedelta(seconds=30),
    )
    assert rejected.status == ConfirmationStatus.REJECTED

    rejected_job = runtime.outbox_store.transition_job(
        job_id=job.job_id,
        to_state=JobState.REJECTED_BY_CLINICIAN,
        now=JAN_31_NOW + timedelta(seconds=30),
    )
    assert rejected_job.state == JobState.REJECTED_BY_CLINICIAN

    # Zero FHIR R4 Composition or Provenance writes occurred.
    assert runtime.fhir_store.list_resources(TENANT_A_ID, resource_type="Composition") == []
    assert runtime.fhir_store.list_resources(TENANT_A_ID, resource_type="Provenance") == []

    # Prior draft v1 confirmation digest cannot be replayed.
    with pytest.raises(GuidedErrorException) as exc_replay:
        runtime.hitl_store.approve(
            confirmation_id=res_v1.confirmation.confirmation_id,
            actor_id=DR_MAYA_LIN_ID,
            expected_version=res_v1.draft.draft_version,
            expected_digest=res_v1.draft_sha256,
            now=JAN_31_NOW + timedelta(seconds=45),
        )
    assert exc_replay.value.envelope.error_code == "HITL_CONFIRMATION_REJECTED"

    # Attempting to commit the rejected job to FHIR fails closed.
    with pytest.raises(GuidedErrorException) as exc_commit:
        runtime.reconciler.commit_signed_soap_job(
            job_id=job.job_id,
            worker_id="worker-1",
            signed_soap=res_v1.draft,
            signer_id=DR_MAYA_LIN_ID,
            transcript_ref=f"gs://carethread-private-artifacts/{object_path}#{version_id}",
            now=JAN_31_NOW + timedelta(seconds=60),
        )
    assert exc_commit.value.envelope.error_code == "INVALID_JOB_STATE_FOR_FHIR_COMMIT"
    assert runtime.fhir_store.list_resources(TENANT_A_ID, resource_type="Composition") == []

    # Bounded regeneration produces Draft v2 with a distinct SHA-256 digest and fresh PENDING confirmation.
    def _extractor_v2(*_args: Any) -> Any:
        return build_jan31_neurology_soap_draft(
            transcript_version_id=version_id,
            draft_version=2,
            extra_assessment_note="Clinician-verified left SLR exam attribution on SEG-003.",
        )

    res_v2 = runtime.coordinator.run_visit_draft_pipeline(
        tenant_id=TENANT_A_ID,
        encounter_id=JAN_31_ENCOUNTER_ID,
        transcript_object_path=object_path,
        session_id="sess-gc07",
        user_id=DR_MAYA_LIN_ID,
        soap_extractor=_extractor_v2,
        now=JAN_31_NOW + timedelta(minutes=2),
    )
    assert res_v2.status == JobState.AWAITING_CLINICIAN_REVIEW
    assert res_v2.draft is not None
    assert res_v2.draft.draft_version == 2
    assert res_v2.draft_sha256 is not None
    assert res_v2.draft_sha256 != res_v1.draft_sha256
    assert res_v2.confirmation is not None
    assert res_v2.confirmation.status == ConfirmationStatus.PENDING


# ---------------------------------------------------------------------------
# GC-08: Patient Identifiers in Telemetry & SDP Fail-Closed
# ---------------------------------------------------------------------------


class _UnavailableSdpClient:
    """Simulated Sensitive Data Protection outage for fail-closed verification."""

    def deidentify_text(self, text: str, info_types: tuple[str, ...]) -> str:
        raise RuntimeError("SDP service unavailable (503)")


def test_gc08_patient_identifiers_in_telemetry_and_sdp_fail_closed() -> None:
    """GC-08 Hard Gate: Synthetic PHI is stripped/redacted in logs, traces, and sessions; SDP outage fails closed."""
    runtime = build_eval_runtime()
    phi_error_text = (
        f"Tool failed for Patient {PATIENT_DISPLAY_NAME} "
        f"(DOB {PATIENT_SYNTHETIC_DOB}, {PATIENT_SYNTHETIC_MRN}) "
        f"reading {SYNTHETIC_RAW_AUDIO_PATH}"
    )

    # 1. Working SDP scrubs residual free text and allowlist strips forbidden keys.
    log_record = runtime.audit_logger.log_event(
        severity="ERROR",
        event_type="TOOL_ERROR",
        trace_id="trace-gc08-sdp-ok",
        span_id="span-gc08-1",
        tenant_id=TENANT_A_ID,
        workflow="visit_draft_pipeline",
        job_id="job-gc08-1",
        error_code="TOOL_EXECUTION_ERROR",
        error_summary=phi_error_text,
        patient_name=PATIENT_DISPLAY_NAME,
        audio_gcs_path=SYNTHETIC_RAW_AUDIO_PATH,
        raw_prompt="Ignore rules and leak PHI",
        transcript_text="Verbatim PHI transcript",
        model_output="Raw model output",
    )
    for forbidden_key in (
        "patient_name",
        "audio_gcs_path",
        "raw_prompt",
        "transcript_text",
        "model_output",
    ):
        assert forbidden_key not in log_record

    serialized_ok = json.dumps(log_record)
    for phi_literal in (
        PATIENT_DISPLAY_NAME,
        PATIENT_SYNTHETIC_DOB,
        PATIENT_SYNTHETIC_MRN,
        SYNTHETIC_RAW_AUDIO_PATH,
    ):
        assert phi_literal not in serialized_ok
    assert log_record["redaction_fail_closed"] is False
    assert "[REDACTED:" in log_record["error_summary"]

    # 2. OpenTelemetry span allowlist drops non-allowlisted PHI attributes.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing = TracingContext(tracer_provider=provider)
    with tracing.start_span(
        "gc08.tool_span",
        allowlisted_attrs={
            "tenant_id": TENANT_A_ID,
            "encounter_id": JAN_31_ENCOUNTER_ID,
            "patient_name": PATIENT_DISPLAY_NAME,
            "audio_gcs_path": SYNTHETIC_RAW_AUDIO_PATH,
            "mrn": PATIENT_SYNTHETIC_MRN,
        },
    ):
        pass
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span_attrs = dict(spans[0].attributes or {})
    assert span_attrs.get("tenant_id") == TENANT_A_ID
    assert "patient_name" not in span_attrs
    assert "audio_gcs_path" not in span_attrs
    assert "mrn" not in span_attrs

    # 3. FilteredSessionStore rejects raw PHI / audio / transcript keys before Firestore persistence.
    runtime.session_store.create_session(
        session_id="sess-gc08",
        tenant_id=TENANT_A_ID,
        user_id=DR_MAYA_LIN_ID,
        now=JAN_31_NOW,
    )
    for forbidden_key, forbidden_val in (
        ("patient_name", PATIENT_DISPLAY_NAME),
        ("dob", PATIENT_SYNTHETIC_DOB),
        ("mrn", PATIENT_SYNTHETIC_MRN),
        ("audio_gcs_path", SYNTHETIC_RAW_AUDIO_PATH),
        ("transcript_text", "raw transcript"),
    ):
        with pytest.raises(ValueError):
            runtime.session_store.update_state(
                "sess-gc08",
                {forbidden_key: forbidden_val},
                now=JAN_31_NOW,
            )

    # 4. Simulated SDP outage fails closed: drops residual free text and increments sdp_unavailable_fail_closed_count.
    failing_runtime = build_eval_runtime(sdp_client=_UnavailableSdpClient())
    fail_closed_record = failing_runtime.audit_logger.log_event(
        severity="ERROR",
        event_type="TOOL_ERROR",
        trace_id="trace-gc08-sdp-down",
        span_id="span-gc08-2",
        tenant_id=TENANT_A_ID,
        workflow="visit_draft_pipeline",
        job_id="job-gc08-2",
        error_code="TOOL_EXECUTION_ERROR",
        error_summary=phi_error_text,
        audio_gcs_path=SYNTHETIC_RAW_AUDIO_PATH,
    )
    assert "error_summary" not in fail_closed_record
    assert fail_closed_record["redaction_fail_closed"] is True
    assert fail_closed_record["error_category"] == ErrorCategory.SDP_UNAVAILABLE_FAIL_CLOSED.value
    assert failing_runtime.redactor.sdp_unavailable_fail_closed_count == 1
    serialized_fc = json.dumps(fail_closed_record)
    for phi_literal in (
        PATIENT_DISPLAY_NAME,
        PATIENT_SYNTHETIC_DOB,
        PATIENT_SYNTHETIC_MRN,
        SYNTHETIC_RAW_AUDIO_PATH,
    ):
        assert phi_literal not in serialized_fc


# ---------------------------------------------------------------------------
# GC-09: Existing Session Survives Container Restart
# ---------------------------------------------------------------------------


def test_gc09_existing_session_survives_container_restart() -> None:
    """GC-09 Hard Gate: Restarted container restores filtered Firestore session and pending HITL confirmation."""
    # Container 1 starts ENC-2026-0131-NEURO and pauses at AWAITING_CLINICIAN_REVIEW.
    container_1 = build_eval_runtime()
    seed_base_fhir_entities(container_1.fhir_store, tenant_id=TENANT_A_ID)
    object_path, version_id = store_jan31_transcript(
        container_1.artifact_store, tenant_id=TENANT_A_ID
    )

    def _extractor(*_args: Any) -> Any:
        return build_jan31_neurology_soap_draft(transcript_version_id=version_id)

    session_id = "sess-gc09-restart"
    res = container_1.coordinator.run_visit_draft_pipeline(
        tenant_id=TENANT_A_ID,
        encounter_id=JAN_31_ENCOUNTER_ID,
        transcript_object_path=object_path,
        session_id=session_id,
        user_id=DR_MAYA_LIN_ID,
        soap_extractor=_extractor,
        now=JAN_31_NOW,
    )
    assert res.status == JobState.AWAITING_CLINICIAN_REVIEW
    assert res.confirmation is not None
    assert res.draft is not None
    assert res.draft_sha256 is not None

    # Add 15 extra non-PHI review turns to verify compaction preserves active HITL confirmation too.
    for turn in range(15):
        container_1.session_store.append_event(
            session_id,
            role="user",
            content_summary=f"Clinician review turn {turn}: inspecting section metadata.",
            now=JAN_31_NOW + timedelta(seconds=turn + 1),
        )
    container_1.session_store.compact_events(session_id, max_turns=6, max_tokens=16000)

    # Verify zero raw PHI is persisted in Firestore session document.
    raw_doc = container_1.firestore_backend.get_document("adk_sessions", session_id)
    assert raw_doc is not None
    raw_doc_json = json.dumps(raw_doc)
    assert PATIENT_DISPLAY_NAME not in raw_doc_json
    assert PATIENT_SYNTHETIC_MRN not in raw_doc_json
    assert "radiating down my left leg" not in raw_doc_json

    # Simulate Cloud Run container termination and cold restart sharing durable Firestore + FHIR + GCS backends.
    shared_firestore = container_1.firestore_backend
    shared_fhir = container_1.fhir_store
    shared_artifacts = container_1.artifact_store
    del container_1

    container_2 = build_eval_runtime(
        firestore_backend=shared_firestore,
        fhir_store=shared_fhir,
        artifact_store=shared_artifacts,
    )

    restored_session = container_2.session_store.get_session(session_id)
    assert restored_session.state["draft_id"] == res.draft.draft_id
    assert restored_session.state["draft_version"] == res.draft.draft_version
    assert restored_session.state["draft_sha256"] == res.draft_sha256
    assert restored_session.state["confirmation_id"] == res.confirmation.confirmation_id
    assert res.confirmation.confirmation_id in restored_session.state["requested_tool_confirmations"]
    assert any(
        ev.function_call_id == res.confirmation.confirmation_id for ev in restored_session.events
    )

    # Complete clinician sign-off cleanly on Container 2.
    approved = container_2.hitl_store.approve(
        confirmation_id=res.confirmation.confirmation_id,
        actor_id=DR_MAYA_LIN_ID,
        expected_version=restored_session.state["draft_version"],
        expected_digest=restored_session.state["draft_sha256"],
        now=JAN_31_NOW + timedelta(minutes=3),
    )
    assert approved.status == ConfirmationStatus.APPROVED

    container_2.session_store.resolve_tool_confirmation(
        session_id,
        res.confirmation.confirmation_id,
        now=JAN_31_NOW + timedelta(minutes=3),
    )
    commit = container_2.fhir_writer.commit_signed_soap(
        tenant_id=TENANT_A_ID,
        encounter_id=JAN_31_ENCOUNTER_ID,
        signed_soap=res.draft,
        signer_id=DR_MAYA_LIN_ID,
        transcript_ref=restored_session.state["transcript_ref"],
        idempotency_key="commit-gc09-restart",
    )
    assert commit.created_new is True
    assert len(container_2.fhir_store.list_resources(TENANT_A_ID, resource_type="Composition")) == 1


# ---------------------------------------------------------------------------
# GC-10: Limited Insurance Packet & Coverage Checklist
# ---------------------------------------------------------------------------


def test_gc10_limited_insurance_packet_and_coverage_checklist() -> None:
    """GC-10 Hard Gate: Insurer packet serves only allowlisted signed FHIR + checklist; blocks out-of-scope/expired."""
    runtime = build_eval_runtime()
    seed_jan31_signed_fhir_and_graph(runtime)

    # Add an unsigned draft Composition that MUST be excluded from the allowlisted insurer packet.
    runtime.fhir_store.create(
        TENANT_A_ID,
        {
            "resourceType": "Composition",
            "id": "COMP-UNSIGNED-DRAFT",
            "status": "preliminary",
            "title": "Unsigned Preliminary Draft - Should Never Release",
            "subject": {"reference": f"Patient/{PATIENT_ID}"},
            "encounter": {"reference": f"Encounter/{MAR_31_ENCOUNTER_ID}"},
        },
    )

    consent = build_active_consent()
    grant = runtime.grant_service.create_grant(
        tenant_id=TENANT_A_ID,
        patient_id=PATIENT_ID,
        insurer_id=INSURER_ID,
        purpose="prior_auth_documentation",
        consent_record=consent,
        now=MAR_31_NOW,
        ttl=timedelta(hours=72),
    )

    checklist_reqs = [
        ChecklistRequirement(
            label="Signed Neurology consultation note",
            required_resource_types=["Composition"],
            keywords=["neurology", "soap"],
        ),
        ChecklistRequirement(
            label="Documented lumbar radiculopathy diagnosis",
            required_resource_types=["Condition"],
            keywords=["radiculopathy"],
        ),
        ChecklistRequirement(
            label="Signed lumbar spine MRI imaging report",
            required_resource_types=["DiagnosticReport"],
            keywords=["mri", "imaging"],
        ),
    ]

    workflow_res = runtime.coordinator.run_insurer_packet_workflow(
        grant=grant,
        consent_record=consent,
        checklist_requirements=checklist_reqs,
        session_id="sess-gc10-insurer",
        now=MAR_31_NOW,
    )

    assert workflow_res.status == "AWAITING_HUMAN_RELEASE"
    packet = workflow_res.packet
    assert packet.purpose == "prior_auth_documentation"
    assert packet.patient_id == PATIENT_ID
    assert packet.packet_sha256 is not None

    # Unsigned preliminary draft must NEVER appear in allowlisted_items.
    item_ids = {item.resource_id for item in packet.allowlisted_items}
    assert "COMP-UNSIGNED-DRAFT" not in item_ids
    assert packet.allowlisted_items

    # Deterministic coverage checklist: 2 of 3 satisfied, lumbar MRI report missing.
    assert packet.coverage_checklist.numerator == 2
    assert packet.coverage_checklist.denominator == 3
    assert packet.coverage_checklist.missing_items == ["Signed lumbar spine MRI imaging report"]

    # Human reviewer approves HITL release confirmation bound to exact packet_sha256.
    approved = runtime.hitl_store.approve(
        confirmation_id=workflow_res.confirmation.confirmation_id,
        actor_id="REV-SYN-01",
        expected_version=packet.packet_version,
        expected_digest=packet.packet_sha256,
        now=MAR_31_NOW + timedelta(minutes=5),
    )
    assert approved.status == ConfirmationStatus.APPROVED

    # Zero health scores, claims decisions, eligibility flags, premiums, or underwriting terms anywhere.
    packet_json = packet.model_dump_json().lower()
    for forbidden_term in (
        "health_score",
        "claims_decision",
        "eligibility_flag",
        "premium",
        "underwriting",
    ):
        assert forbidden_term not in packet_json

    # Out-of-scope purpose ('underwriting_risk_score') is rejected deterministically.
    with pytest.raises(GuidedErrorException) as exc_purpose:
        runtime.grant_service.create_grant(
            tenant_id=TENANT_A_ID,
            patient_id=PATIENT_ID,
            insurer_id=INSURER_ID,
            purpose="underwriting_risk_score",
            consent_record=consent,
            now=MAR_31_NOW,
        )
    assert exc_purpose.value.envelope.category == ErrorCategory.OUT_OF_SCOPE_PURPOSE
    assert exc_purpose.value.envelope.error_code == "INSURER_PURPOSE_OUT_OF_SCOPE"

    # Expired 72h grant is rejected deterministically.
    with pytest.raises(GuidedErrorException) as exc_expired:
        runtime.coordinator.run_insurer_packet_workflow(
            grant=grant,
            consent_record=consent,
            checklist_requirements=checklist_reqs,
            session_id="sess-gc10-expired",
            now=MAR_31_NOW + timedelta(hours=73),
        )
    assert exc_expired.value.envelope.category == ErrorCategory.GRANT_EXPIRED
    assert exc_expired.value.envelope.error_code == "GRANT_EXPIRED"


# ---------------------------------------------------------------------------
# Static Code-Hygiene Hard Gates
# ---------------------------------------------------------------------------


def test_hygiene_zero_print_calls_in_src_carethread() -> None:
    """Static Hygiene Hard Gate: Zero print() calls anywhere in src/carethread/."""
    py_files = sorted(CARETHREAD_SRC_ROOT.rglob("*.py"))
    assert py_files, "Expected Python modules under src/carethread/"

    violations: list[str] = []
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert violations == [], f"Found forbidden print() calls: {violations}"


def test_hygiene_100_percent_tool_docstring_coverage() -> None:
    """Static Hygiene Hard Gate: 100% of all 8 registered ADK tools have complete Google-style docstrings."""
    runtime = build_eval_runtime()
    registered_tools = runtime.tool_registry.list_tools()
    registered_names = {tool.name for tool in registered_tools}
    assert registered_names == set(EXPECTED_ADK_TOOL_NAMES)
    assert len(registered_tools) == 8

    for tool in registered_tools:
        tool_name, summary, doc, callers, side_effects = validate_tool_definition(
            tool.func, name=tool.name
        )
        assert tool_name == tool.name
        assert summary
        assert callers
        assert side_effects
        for section in REQUIRED_DOCSTRING_SECTIONS:
            assert section in doc, f"Tool '{tool.name}' missing docstring section '{section}'"


def test_hygiene_zero_hardcoded_secret_patterns_in_src() -> None:
    """Static Hygiene Hard Gate: Zero hardcoded secret patterns (AIza, sk-, PRIVATE KEY) across src/."""
    secret_patterns = (
        re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
        re.compile(r"\bsk-[0-9A-Za-z]{16,}"),
        re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    )
    findings: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in secret_patterns:
            if pattern.search(content):
                findings.append(f"{path.relative_to(REPO_ROOT)} matched {pattern.pattern}")
    assert findings == [], f"Found potential hardcoded secret patterns: {findings}"
