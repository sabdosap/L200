"""Tests for derived Spanner Graph index, bounded continuity retrieval, and insurer packet assembler."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json

import pytest

from carethread.errors import ErrorCategory, GuidedErrorException
from carethread.schemas.v1.continuity import ContinuitySource, CurrentEncounterContext
from carethread.schemas.v1.insurer import CoverageChecklist
from carethread.schemas.v1.soap import (
    EncounterRef,
    MedicationItem,
    MedicationStatus,
    SoapDraftPayload,
    SoapItem,
    SoapPlan,
    SoapSections,
    SourceRefs,
    SpeakerAttribution,
    compute_canonical_sha256,
)
from carethread.security import (
    ActorContext,
    AuthzService,
    ConsentRecord,
    ConsentStatus,
    GrantService,
    InMemorySecretManagerClient,
)
from carethread.services.continuity import ContinuityService
from carethread.services.insurer_release import InsurerReleaseService
from carethread.stores.fhir import DeterministicFhirWriter, InMemoryFhirR4Store
from carethread.stores.spanner_graph import (
    InMemorySpannerGraphStore,
    SpannerGraphUnavailableError,
)

PROJECT_ID = "carethread-test-project"
SECRET_ID = "scope-token-hmac-key"
SECRET_VERSION = "1"
SIGNING_KEY = b"test-only-hmac-key-material-32bytes"

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
PATIENT_ID = "PT-SYN-0001"
ENCOUNTER_JAN31_ID = "ENC-2026-01-31-NEURO"
PRACTITIONER_ID = "PRAC-NEURO-01"
ORG_ID = "ORG-NEURO-CLINIC"


def _now() -> datetime:
    return datetime(2026, 2, 20, 10, 0, 0, tzinfo=timezone.utc)


def _make_authz_service() -> AuthzService:
    secrets = InMemorySecretManagerClient(
        secrets={(PROJECT_ID, SECRET_ID, SECRET_VERSION): SIGNING_KEY}
    )
    return AuthzService(
        secret_manager=secrets,
        secret_project_id=PROJECT_ID,
        secret_id=SECRET_ID,
        secret_version=SECRET_VERSION,
    )


def _active_consent(
    *,
    tenant_id: str = TENANT_A,
    patient_id: str = PATIENT_ID,
    consent_version_id: str = "CV-2026-01",
    status: ConsentStatus = ConsentStatus.ACTIVE,
) -> ConsentRecord:
    return ConsentRecord(
        consent_version_id=consent_version_id,
        tenant_id=tenant_id,
        patient_id=patient_id,
        status=status,
        allowed_categories=frozenset(
            {
                "clinical_summary",
                "medications",
                "conditions",
                "observations",
                "diagnostic_reports",
                "provenance",
            }
        ),
    )


def _make_scope_token(
    authz: AuthzService,
    *,
    tenant_id: str = TENANT_A,
    patient_id: str = PATIENT_ID,
    now: datetime | None = None,
):
    actor = ActorContext(
        actor_id="CLIN-ORTHO-01",
        tenant_id=tenant_id,
        role="CLINICIAN",
        specialty="Orthopedics",
        care_patient_ids=frozenset({patient_id}),
    )
    consent = _active_consent(tenant_id=tenant_id, patient_id=patient_id)
    return authz.authorize_clinical_access(
        actor=actor,
        patient_id=patient_id,
        patient_tenant_id=tenant_id,
        consent_record=consent,
        now=now or _now(),
    )


def _seed_jan31_neurology_fhir(
    fhir_store: InMemoryFhirR4Store,
    *,
    tenant_id: str = TENANT_A,
    patient_id: str = PATIENT_ID,
    encounter_id: str = ENCOUNTER_JAN31_ID,
) -> dict[str, str]:
    """Seed an authoritative Jan 31 Neurology encounter and signed SOAP note into FHIR R4."""
    fhir_store.create(
        tenant_id,
        {
            "resourceType": "Patient",
            "id": patient_id,
            "active": True,
        },
    )
    fhir_store.create(
        tenant_id,
        {
            "resourceType": "Organization",
            "id": ORG_ID,
            "name": "Metro Neurology Clinic",
        },
    )
    fhir_store.create(
        tenant_id,
        {
            "resourceType": "Practitioner",
            "id": PRACTITIONER_ID,
            "name": [{"text": "Dr. Priya Nair"}],
        },
    )
    fhir_store.create(
        tenant_id,
        {
            "resourceType": "PractitionerRole",
            "id": f"ROLE-{PRACTITIONER_ID}",
            "practitioner": {"reference": f"Practitioner/{PRACTITIONER_ID}"},
            "organization": {"reference": f"Organization/{ORG_ID}"},
            "specialty": [{"coding": [{"code": "neurology", "display": "Neurology"}], "text": "Neurology"}],
        },
    )
    fhir_store.create(
        tenant_id,
        {
            "resourceType": "Encounter",
            "id": encounter_id,
            "status": "finished",
            "subject": {"reference": f"Patient/{patient_id}"},
            "period": {"start": "2026-01-31T09:30:00+00:00", "end": "2026-01-31T10:15:00+00:00"},
            "participant": [{"individual": {"reference": f"Practitioner/{PRACTITIONER_ID}"}}],
            "serviceType": {"text": "Neurology"},
            "serviceProvider": {"reference": f"Organization/{ORG_ID}"},
        },
    )

    signed_soap = SoapDraftPayload(
        draft_id="DRAFT-JAN31-01",
        draft_version=1,
        status="SIGNED",
        encounter_ref=EncounterRef(
            encounter_id=encounter_id,
            encounter_date=date(2026, 1, 31),
            specialty="Neurology",
        ),
        source_refs=SourceRefs(
            transcript_version_id="TR-JAN31-V1",
            fhir_prior_refs=[],
        ),
        speaker_attribution=SpeakerAttribution(status="VERIFIED"),
        soap=SoapSections(
            subjective=[
                SoapItem(
                    item_id="S1",
                    category="chief_complaint",
                    statement="Radiating low back pain down left leg for 4 weeks.",
                    polarity="PRESENT",
                    evidence_ids=["E1"],
                )
            ],
            objective=[
                SoapItem(
                    item_id="O1",
                    category="physical_exam",
                    statement="Positive left straight leg raise at 45 degrees; conservative physical therapy initiated.",
                    polarity="PRESENT",
                    evidence_ids=["E2"],
                )
            ],
            assessment=[
                SoapItem(
                    item_id="A1",
                    category="diagnosis",
                    statement="Lumbar radiculopathy",
                    polarity="PRESENT",
                    certainty="confirmed",
                    evidence_ids=["E3"],
                )
            ],
            plan=SoapPlan(
                medications=[
                    MedicationItem(
                        item_id="M1",
                        medication_name="Gabapentin",
                        dose=300,
                        unit="mg",
                        route="oral",
                        frequency="nightly",
                        event_date=date(2026, 1, 31),
                        medication_status=MedicationStatus.DISCUSSED,
                        status_rationale="Discussed as an option if conservative therapy fails; not prescribed.",
                        evidence_ids=["E4"],
                    )
                ],
                lifestyle_and_activity=[
                    SoapItem(
                        item_id="P1",
                        category="physical_therapy",
                        statement="Continue structured physical therapy for 4 weeks.",
                        polarity="PRESENT",
                        evidence_ids=["E5"],
                    )
                ],
                follow_up=[
                    SoapItem(
                        item_id="F1",
                        category="follow_up",
                        statement="Follow up with Orthopedics if radicular pain persists.",
                        polarity="PRESENT",
                        evidence_ids=["E6"],
                    )
                ],
            ),
        ),
    )

    writer = DeterministicFhirWriter(fhir_store)
    commit = writer.commit_signed_soap(
        tenant_id=tenant_id,
        encounter_id=encounter_id,
        signed_soap=signed_soap,
        signer_id=PRACTITIONER_ID,
        transcript_ref="TR-JAN31-V1",
        idempotency_key=f"idem-{encounter_id}",
    )
    comp_id = commit.composition_ref.split("/")[1]
    return {
        "composition_id": comp_id,
        "composition_ref": commit.composition_ref,
        "provenance_ref": commit.provenance_ref,
    }


def test_healthy_spanner_graph_returns_verified_jan31_neurology_history():
    fhir_store = InMemoryFhirR4Store()
    seeded = _seed_jan31_neurology_fhir(fhir_store)

    graph_store = InMemorySpannerGraphStore()
    graph_store.rebuild_from_fhir(TENANT_A, fhir_store)

    authz = _make_authz_service()
    token = _make_scope_token(authz)
    service = ContinuityService(fhir_store=fhir_store, graph_store=graph_store, authz_service=authz)

    result = service.get_verified_patient_history(
        scope_token=token,
        as_of_date=date(2026, 2, 20),
        max_encounters=10,
        now=_now(),
    )

    assert result.continuity_source == ContinuitySource.SPANNER_GRAPH_VERIFIED
    assert result.staleness_banner is None
    assert result.graph_watermark_ts is not None
    assert any(enc.encounter_id == ENCOUNTER_JAN31_ID for enc in result.encounters)
    assert any(
        enc.encounter_date == date(2026, 1, 31) and enc.specialty == "Neurology"
        for enc in result.encounters
    )

    med_items = [item for item in result.prior_history_items if item.category == "medication"]
    assert len(med_items) == 1
    assert med_items[0].medication_status == MedicationStatus.DISCUSSED
    assert med_items[0].encounter_date == date(2026, 1, 31)
    assert "Gabapentin" in med_items[0].summary

    problem_items = [item for item in result.prior_history_items if item.category == "problem"]
    assert len(problem_items) == 1
    assert "Lumbar radiculopathy" in problem_items[0].summary
    assert problem_items[0].encounter_date == date(2026, 1, 31)

    comp_refs = [
        ref
        for ref in result.fhir_version_refs
        if ref.resource_type == "Composition" and ref.resource_id == seeded["composition_id"]
    ]
    assert len(comp_refs) == 1
    assert comp_refs[0].version_id == "1"

    brief = result.to_continuity_brief(
        CurrentEncounterContext(
            encounter_id="ENC-2026-02-20-ORTHO",
            encounter_date=date(2026, 2, 20),
            specialty="Orthopedics",
        )
    )
    assert brief.continuity_source == ContinuitySource.SPANNER_GRAPH_VERIFIED
    assert len(brief.prior_history_items) >= 2


def test_stale_graph_composition_version_triggers_authoritative_fhir_fallback():
    fhir_store = InMemoryFhirR4Store()
    seeded = _seed_jan31_neurology_fhir(fhir_store)

    graph_store = InMemorySpannerGraphStore()
    graph_store.rebuild_from_fhir(TENANT_A, fhir_store)

    # Amend Composition in authoritative FHIR R4 to versionId=2 while graph still holds versionId=1.
    comp_v1 = fhir_store.read(TENANT_A, "Composition", seeded["composition_id"])
    comp_v1["title"] = "Amended Signed SOAP Note v2 - Updated neurology addendum"
    comp_v2 = fhir_store.update(TENANT_A, comp_v1, if_match='W/"1"')
    assert comp_v2["meta"]["versionId"] == "2"

    authz = _make_authz_service()
    token = _make_scope_token(authz)
    service = ContinuityService(fhir_store=fhir_store, graph_store=graph_store, authz_service=authz)

    result = service.get_verified_patient_history(
        scope_token=token,
        as_of_date=date(2026, 2, 20),
        max_encounters=10,
        now=_now(),
    )

    assert result.continuity_source == ContinuitySource.FHIR_R4_AUTHORITATIVE
    assert result.staleness_banner is not None
    assert result.fallback_reason == "STALE_FHIR_VERSION"

    comp_refs = [
        ref
        for ref in result.fhir_version_refs
        if ref.resource_type == "Composition" and ref.resource_id == seeded["composition_id"]
    ]
    assert len(comp_refs) == 1
    assert comp_refs[0].version_id == "2"

    comp_items = [
        item
        for item in result.prior_history_items
        if item.fhir_ref is not None and item.fhir_ref.resource_type == "Composition"
    ]
    assert len(comp_items) == 1
    assert "Amended Signed SOAP Note v2" in comp_items[0].summary
    assert comp_items[0].fhir_ref.version_id == "2"


def test_spanner_graph_timeout_and_error_trigger_authoritative_fhir_fallback():
    fhir_store = InMemoryFhirR4Store()
    _seed_jan31_neurology_fhir(fhir_store)

    # Case 1: Simulated Spanner Graph latency exceeding 1500 ms timeout budget.
    slow_graph = InMemorySpannerGraphStore(simulated_latency_ms=1600, timeout_ms=1500)
    slow_graph.rebuild_from_fhir(TENANT_A, fhir_store)

    authz = _make_authz_service()
    token = _make_scope_token(authz)
    service = ContinuityService(fhir_store=fhir_store, graph_store=slow_graph, authz_service=authz)

    timeout_result = service.get_verified_patient_history(
        scope_token=token,
        as_of_date=date(2026, 2, 20),
        max_encounters=10,
        now=_now(),
    )

    assert timeout_result.continuity_source == ContinuitySource.FHIR_R4_AUTHORITATIVE
    assert timeout_result.staleness_banner is not None
    assert timeout_result.fallback_reason == "SPANNER_GRAPH_TIMEOUT"
    assert any(enc.encounter_id == ENCOUNTER_JAN31_ID for enc in timeout_result.encounters)

    # Case 2: Simulated Spanner Graph backend error.
    failing_graph = InMemorySpannerGraphStore(
        simulate_error=SpannerGraphUnavailableError("Simulated Spanner outage")
    )
    failing_service = ContinuityService(
        fhir_store=fhir_store, graph_store=failing_graph, authz_service=authz
    )
    error_result = failing_service.get_verified_patient_history(
        scope_token=token,
        as_of_date=date(2026, 2, 20),
        max_encounters=10,
        now=_now(),
    )
    assert error_result.continuity_source == ContinuitySource.FHIR_R4_AUTHORITATIVE
    assert error_result.fallback_reason == "SPANNER_GRAPH_ERROR"
    assert any(enc.encounter_id == ENCOUNTER_JAN31_ID for enc in error_result.encounters)


def test_full_graph_rebuild_from_fhir_is_idempotent_and_tenant_isolated():
    fhir_store = InMemoryFhirR4Store()
    _seed_jan31_neurology_fhir(fhir_store, tenant_id=TENANT_A, patient_id=PATIENT_ID)
    _seed_jan31_neurology_fhir(
        fhir_store,
        tenant_id=TENANT_B,
        patient_id=PATIENT_ID,
        encounter_id="ENC-TENANT-B-01",
    )

    graph_store = InMemorySpannerGraphStore()
    stats_first = graph_store.rebuild_from_fhir(TENANT_A, fhir_store)
    graph_store.rebuild_from_fhir(TENANT_B, fhir_store)
    snap_first = graph_store.query_patient_continuity(TENANT_A, PATIENT_ID, max_encounters=10, max_hops=2)

    stats_second = graph_store.rebuild_from_fhir(TENANT_A, fhir_store)
    snap_second = graph_store.query_patient_continuity(TENANT_A, PATIENT_ID, max_encounters=10, max_hops=2)

    assert stats_first.encounters == stats_second.encounters == 1
    assert stats_first.medication_events == stats_second.medication_events == 1
    assert stats_first.problem_events == stats_second.problem_events == 1

    # Compare structural equality ignoring watermark timestamp
    assert snap_first.encounters == snap_second.encounters
    assert snap_first.medication_events == snap_second.medication_events
    assert snap_first.problem_events == snap_second.problem_events
    assert snap_first.fhir_version_refs == snap_second.fhir_version_refs

    # Tenant isolation: Tenant A never sees Tenant B's encounter
    assert [enc.encounter_id for enc in snap_second.encounters] == [ENCOUNTER_JAN31_ID]
    snap_b = graph_store.query_patient_continuity(TENANT_B, PATIENT_ID, max_encounters=10, max_hops=2)
    assert [enc.encounter_id for enc in snap_b.encounters] == ["ENC-TENANT-B-01"]


def test_graph_bounds_and_amendment_is_current_false():
    fhir_store = InMemoryFhirR4Store()
    seeded = _seed_jan31_neurology_fhir(fhir_store)

    graph_store = InMemorySpannerGraphStore()
    graph_store.rebuild_from_fhir(TENANT_A, fhir_store)

    # Bound violations fail deterministically
    with pytest.raises(GuidedErrorException) as exc_enc:
        graph_store.query_patient_continuity(TENANT_A, PATIENT_ID, max_encounters=26, max_hops=2)
    assert exc_enc.value.envelope.category == ErrorCategory.VALIDATION_RECOVERABLE

    with pytest.raises(GuidedErrorException) as exc_hops:
        graph_store.query_patient_continuity(TENANT_A, PATIENT_ID, max_encounters=10, max_hops=3)
    assert exc_hops.value.envelope.category == ErrorCategory.VALIDATION_RECOVERABLE

    # Amending a resource marks old projection is_current=False
    comp_v1 = fhir_store.read(TENANT_A, "Composition", seeded["composition_id"])
    comp_v1["title"] = "Amended Signed SOAP Note v2"
    fhir_store.update(TENANT_A, comp_v1, if_match='W/"1"')
    graph_store.mark_resource_amended(
        TENANT_A, "Composition", seeded["composition_id"], superseded_version_id="1"
    )
    snap_after_amend = graph_store.query_patient_continuity(TENANT_A, PATIENT_ID, max_encounters=10)
    assert snap_after_amend.encounters == []


def test_continuity_service_rejects_expired_scope_token():
    fhir_store = InMemoryFhirR4Store()
    _seed_jan31_neurology_fhir(fhir_store)
    graph_store = InMemorySpannerGraphStore()
    graph_store.rebuild_from_fhir(TENANT_A, fhir_store)

    authz = _make_authz_service()
    token = _make_scope_token(authz, now=_now())
    service = ContinuityService(fhir_store=fhir_store, graph_store=graph_store, authz_service=authz)

    with pytest.raises(GuidedErrorException) as exc_info:
        service.get_verified_patient_history(
            scope_token=token,
            as_of_date=date(2026, 2, 20),
            max_encounters=10,
            now=_now() + timedelta(hours=1),
        )
    assert exc_info.value.envelope.category == ErrorCategory.AUTHZ_DENIED


def test_insurer_packet_contains_only_allowlisted_signed_fhir_and_deterministic_checklist():
    fhir_store = InMemoryFhirR4Store()
    _seed_jan31_neurology_fhir(fhir_store)

    # Add non-allowlisted / unsigned / forbidden artifacts into FHIR store to prove exclusion:
    # 1. Unsigned draft Composition (status=preliminary)
    fhir_store.create(
        TENANT_A,
        {
            "resourceType": "Composition",
            "id": "COMP-DRAFT-UNVERIFIED",
            "status": "preliminary",
            "title": "DRAFT MRI lumbar spine report unverified",
            "subject": {"reference": f"Patient/{PATIENT_ID}"},
            "encounter": {"reference": f"Encounter/{ENCOUNTER_JAN31_ID}"},
        },
    )
    # 2. Raw audio Binary resource
    fhir_store.create(
        TENANT_A,
        {
            "resourceType": "Binary",
            "id": "AUDIO-RAW-001",
            "contentType": "audio/wav",
            "data": "UklGRiQAAABXQVZF",
            "subject": {"reference": f"Patient/{PATIENT_ID}"},
        },
    )
    # 3. Session state / health score custom resource
    fhir_store.create(
        TENANT_A,
        {
            "resourceType": "Basic",
            "id": "SESSION-STATE-001",
            "code": {"text": "session_state health_score=87"},
            "subject": {"reference": f"Patient/{PATIENT_ID}"},
        },
    )

    consent = _active_consent()
    grant_service = GrantService()
    grant = grant_service.create_grant(
        tenant_id=TENANT_A,
        patient_id=PATIENT_ID,
        insurer_id="INS-SYN-01",
        purpose="prior_auth_documentation",
        consent_record=consent,
        now=_now(),
    )

    requirements = [
        "Neurology SOAP Note",
        "Lumbar radiculopathy diagnosis",
        "Gabapentin medication",
        "Conservative physical therapy",
        "MRI lumbar spine report",
    ]

    release_service = InsurerReleaseService(fhir_store=fhir_store, grant_service=grant_service)
    packet = release_service.assemble_allowlisted_packet(
        grant=grant,
        consent=consent,
        checklist_requirements=requirements,
        now=_now() + timedelta(hours=1),
    )

    assert packet.grant_id == grant.grant_id
    assert packet.patient_id == PATIENT_ID
    assert packet.consent_version_id == consent.consent_version_id
    assert packet.purpose == "prior_auth_documentation"
    assert packet.coverage_checklist == CoverageChecklist(
        numerator=4,
        denominator=5,
        missing_items=["MRI lumbar spine report"],
    )

    # Verify zero drafts, raw audio, session state, or health scores leaked
    item_ids = {item.resource_id for item in packet.allowlisted_items}
    item_types = {item.resource_type for item in packet.allowlisted_items}
    serialized = json.dumps(packet.model_dump(mode="json")).lower()

    assert "COMP-DRAFT-UNVERIFIED" not in item_ids
    assert "AUDIO-RAW-001" not in item_ids
    assert "SESSION-STATE-001" not in item_ids
    assert "Binary" not in item_types
    assert "Basic" not in item_types
    assert "audio/wav" not in serialized
    assert "session_state" not in serialized
    assert "health_score" not in serialized
    assert "preliminary" not in serialized

    # Verify canonical packet_sha256
    assert packet.packet_sha256 is not None
    expected_sha = compute_canonical_sha256(packet.model_copy(update={"packet_sha256": None}))
    assert packet.packet_sha256 == expected_sha


def test_insurer_packet_rejects_expired_grant_and_revoked_consent():
    fhir_store = InMemoryFhirR4Store()
    _seed_jan31_neurology_fhir(fhir_store)

    consent = _active_consent()
    grant_service = GrantService()
    grant = grant_service.create_grant(
        tenant_id=TENANT_A,
        patient_id=PATIENT_ID,
        insurer_id="INS-SYN-01",
        purpose="prior_auth_documentation",
        consent_record=consent,
        now=_now(),
    )
    release_service = InsurerReleaseService(fhir_store=fhir_store, grant_service=grant_service)

    # Expired grant (> 72h)
    with pytest.raises(GuidedErrorException) as exc_exp:
        release_service.assemble_allowlisted_packet(
            grant=grant,
            consent=consent,
            checklist_requirements=["Neurology SOAP Note"],
            now=_now() + timedelta(hours=73),
        )
    assert exc_exp.value.envelope.category == ErrorCategory.GRANT_EXPIRED

    # Revoked consent
    revoked_consent = consent.model_copy(update={"status": ConsentStatus.REVOKED})
    with pytest.raises(GuidedErrorException) as exc_rev:
        release_service.assemble_allowlisted_packet(
            grant=grant,
            consent=revoked_consent,
            checklist_requirements=["Neurology SOAP Note"],
            now=_now() + timedelta(hours=1),
        )
    assert exc_rev.value.envelope.category == ErrorCategory.CONSENT_REVOKED

    # Non-allowlisted purpose
    bad_purpose_grant = grant.model_copy(update={"purpose": "underwriting_risk_scoring"})
    with pytest.raises(GuidedErrorException) as exc_purp:
        release_service.assemble_allowlisted_packet(
            grant=bad_purpose_grant,
            consent=consent,
            checklist_requirements=["Neurology SOAP Note"],
            now=_now() + timedelta(hours=1),
        )
    assert exc_purp.value.envelope.category == ErrorCategory.OUT_OF_SCOPE_PURPOSE


def test_missing_graph_projection_triggers_authoritative_fhir_fallback():
    fhir_store = InMemoryFhirR4Store()
    _seed_jan31_neurology_fhir(fhir_store)
    empty_graph = InMemorySpannerGraphStore()

    authz = _make_authz_service()
    token = _make_scope_token(authz)
    service = ContinuityService(fhir_store=fhir_store, graph_store=empty_graph, authz_service=authz)

    result = service.get_verified_patient_history(
        scope_token=token,
        as_of_date=date(2026, 2, 20),
        max_encounters=10,
        now=_now(),
    )
    assert result.continuity_source == ContinuitySource.FHIR_R4_AUTHORITATIVE
    assert result.fallback_reason == "MISSING_GRAPH_PROJECTION"
    assert any(enc.encounter_id == ENCOUNTER_JAN31_ID for enc in result.encounters)
