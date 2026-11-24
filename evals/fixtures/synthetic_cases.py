"""100% fabricated synthetic clinical and insurer fixtures for golden evaluations (GC-01..GC-10).

Zero real patient data is used anywhere in this module. All identifiers, names, dates,
transcripts, and insurer names are synthetic evaluation fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from typing import Any

from carethread.agents.coordinator import CareThreadCoordinator
from carethread.hitl.confirmations import HitlConfirmationStore
from carethread.observability.logger import StructuredAuditLogger
from carethread.observability.redaction import FailClosedRedactor, LocalRegexSdpClient, SdpClient
from carethread.plugins.policy_plugin import CareThreadPolicyPlugin
from carethread.routing.model_router import ModelRouter
from carethread.schemas.v1.continuity import CurrentEncounterContext
from carethread.schemas.v1.soap import (
    EncounterRef,
    MedicationItem,
    MedicationStatus,
    ReviewFlag,
    SoapDraftPayload,
    SoapItem,
    SoapPlan,
    SoapSections,
    SourceRefs,
    SpeakerAttribution,
    SpeakerRoleCandidate,
    TranscriptSegment,
)
from carethread.security.authz import ActorContext, AuthzService
from carethread.security.consent import ConsentRecord, ConsentStatus, GrantService
from carethread.security.secrets import InMemorySecretManagerClient
from carethread.services.continuity import ContinuityService
from carethread.services.insurer_release import InsurerReleaseService
from carethread.session.firestore_session import FilteredSessionStore, InMemoryFirestoreBackend
from carethread.stores.fhir import (
    DeterministicFhirWriter,
    FhirCommitResult,
    InMemoryFhirR4Store,
)
from carethread.stores.gcs import InMemoryArtifactStore, build_tenant_artifact_path
from carethread.stores.outbox import FhirCommitReconciler, JobOutboxStore
from carethread.stores.spanner_graph import InMemorySpannerGraphStore
from carethread.tools.clinical_tools import build_clinical_tool_registry
from carethread.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Canonical 100% Fabricated Synthetic Constants
# ---------------------------------------------------------------------------

TENANT_A_ID = "tenant-clinic-a"
TENANT_B_ID = "tenant-clinic-b"

ORG_SYN_A_ID = "ORG-SYN-A"
ORG_SYN_A_NAME = "CareThread Synthetic Multi-Specialty Clinic"

PATIENT_ID = "PT-SYN-0001"
PATIENT_DISPLAY_NAME = "Alex Rivera"
PATIENT_SYNTHETIC_DOB = "1980-05-14"
PATIENT_SYNTHETIC_MRN = "MRN-999-001"
SYNTHETIC_RAW_AUDIO_PATH = (
    "gs://carethread-private-audio/tenants/tenant-clinic-a/audio/ENC-2026-0131-NEURO.wav"
)

JAN_31_DATE = date(2026, 1, 31)
MAR_31_DATE = date(2026, 3, 31)

JAN_31_NOW = datetime(2026, 1, 31, 15, 30, 0, tzinfo=timezone.utc)
MAR_31_NOW = datetime(2026, 3, 31, 14, 0, 0, tzinfo=timezone.utc)

JAN_31_ENCOUNTER_ID = "ENC-2026-0131-NEURO"
MAR_31_ENCOUNTER_ID = "ENC-2026-0331-ORTHO"

DR_MAYA_LIN_ID = "PRAC-SYN-NEURO-01"
DR_MAYA_LIN_NAME = "Dr. Maya Lin"

DR_DAVID_OKAFOR_ID = "PRAC-SYN-ORTHO-01"
DR_DAVID_OKAFOR_NAME = "Dr. David Okafor"

INSURER_ID = "INS-CLEARPATH-01"
INSURER_NAME = "ClearPath Health Plan"

SECRET_PROJECT_ID = "carethread-eval"
SECRET_ID = "scope-token-signing-key"
SECRET_VERSION = "1"
SYNTHETIC_SIGNING_KEY = b"synthetic-eval-hmac-signing-key-32bytes!"

DEFAULT_CLINICAL_CATEGORIES: frozenset[str] = frozenset(
    {
        "clinical_summary",
        "conditions",
        "medications",
        "observations",
        "diagnostic_reports",
        "provenance",
    }
)


@dataclass
class EvalRuntime:
    """End-to-end wired CareThread runtime using real components and in-memory stores."""

    fhir_store: InMemoryFhirR4Store
    graph_store: InMemorySpannerGraphStore
    artifact_store: InMemoryArtifactStore
    firestore_backend: InMemoryFirestoreBackend
    session_store: FilteredSessionStore
    hitl_store: HitlConfirmationStore
    outbox_store: JobOutboxStore
    secret_manager: InMemorySecretManagerClient
    authz_service: AuthzService
    grant_service: GrantService
    continuity_service: ContinuityService
    insurer_release_service: InsurerReleaseService
    redactor: FailClosedRedactor
    audit_sink: list[dict[str, Any]]
    audit_logger: StructuredAuditLogger
    policy_plugin: CareThreadPolicyPlugin
    model_router: ModelRouter
    tool_registry: ToolRegistry
    coordinator: CareThreadCoordinator
    fhir_writer: DeterministicFhirWriter
    reconciler: FhirCommitReconciler


def build_eval_runtime(
    *,
    firestore_backend: InMemoryFirestoreBackend | None = None,
    fhir_store: InMemoryFhirR4Store | None = None,
    graph_store: InMemorySpannerGraphStore | None = None,
    artifact_store: InMemoryArtifactStore | None = None,
    sdp_client: SdpClient | None = None,
) -> EvalRuntime:
    """Assemble a complete real CareThread runtime for golden evaluations."""
    resolved_fhir = fhir_store if fhir_store is not None else InMemoryFhirR4Store()
    resolved_graph = graph_store if graph_store is not None else InMemorySpannerGraphStore()
    resolved_artifacts = artifact_store if artifact_store is not None else InMemoryArtifactStore()
    resolved_backend = (
        firestore_backend if firestore_backend is not None else InMemoryFirestoreBackend()
    )
    session_store = FilteredSessionStore(backend=resolved_backend)
    hitl_store = HitlConfirmationStore(backend=resolved_backend)
    outbox_store = JobOutboxStore()

    secret_manager = InMemorySecretManagerClient(
        {(SECRET_PROJECT_ID, SECRET_ID, SECRET_VERSION): SYNTHETIC_SIGNING_KEY}
    )
    authz_service = AuthzService(
        secret_manager=secret_manager,
        secret_project_id=SECRET_PROJECT_ID,
        secret_id=SECRET_ID,
        secret_version=SECRET_VERSION,
    )
    grant_service = GrantService()
    continuity_service = ContinuityService(
        fhir_store=resolved_fhir,
        graph_store=resolved_graph,
        authz_service=authz_service,
    )
    insurer_release_service = InsurerReleaseService(
        fhir_store=resolved_fhir,
        grant_service=grant_service,
    )

    redactor = FailClosedRedactor(sdp_client=sdp_client or LocalRegexSdpClient())
    audit_sink: list[dict[str, Any]] = []
    audit_logger = StructuredAuditLogger(redactor=redactor, sink=audit_sink)
    policy_plugin = CareThreadPolicyPlugin(
        audit_logger=audit_logger,
        token_verifier=authz_service,
    )
    model_router = ModelRouter()
    tool_registry = build_clinical_tool_registry(
        artifact_store=resolved_artifacts,
        fhir_store=resolved_fhir,
        graph_store=resolved_graph,
        continuity_service=continuity_service,
        insurer_release_service=insurer_release_service,
        hitl_store=hitl_store,
    )
    coordinator = CareThreadCoordinator(
        tool_registry=tool_registry,
        policy_plugin=policy_plugin,
        model_router=model_router,
        session_store=session_store,
        authz_service=authz_service,
        continuity_service=continuity_service,
    )
    fhir_writer = DeterministicFhirWriter(resolved_fhir)
    reconciler = FhirCommitReconciler(
        outbox_store=outbox_store,
        fhir_store=resolved_fhir,
        writer=fhir_writer,
    )
    return EvalRuntime(
        fhir_store=resolved_fhir,
        graph_store=resolved_graph,
        artifact_store=resolved_artifacts,
        firestore_backend=resolved_backend,
        session_store=session_store,
        hitl_store=hitl_store,
        outbox_store=outbox_store,
        secret_manager=secret_manager,
        authz_service=authz_service,
        grant_service=grant_service,
        continuity_service=continuity_service,
        insurer_release_service=insurer_release_service,
        redactor=redactor,
        audit_sink=audit_sink,
        audit_logger=audit_logger,
        policy_plugin=policy_plugin,
        model_router=model_router,
        tool_registry=tool_registry,
        coordinator=coordinator,
        fhir_writer=fhir_writer,
        reconciler=reconciler,
    )


def build_synthetic_actors() -> dict[str, ActorContext]:
    """Return synthetic clinicians for Tenant A (authorized & unauthorized) and Tenant B."""
    return {
        "dr_maya_lin": ActorContext(
            actor_id=DR_MAYA_LIN_ID,
            tenant_id=TENANT_A_ID,
            role="PHYSICIAN",
            specialty="Neurology",
            care_patient_ids=frozenset({PATIENT_ID}),
        ),
        "dr_david_okafor": ActorContext(
            actor_id=DR_DAVID_OKAFOR_ID,
            tenant_id=TENANT_A_ID,
            role="PHYSICIAN",
            specialty="Orthopedics",
            care_patient_ids=frozenset({PATIENT_ID}),
        ),
        "tenant_b_clinician": ActorContext(
            actor_id="PRAC-SYN-TENANTB-01",
            tenant_id=TENANT_B_ID,
            role="PHYSICIAN",
            specialty="Orthopedics",
            care_patient_ids=frozenset({PATIENT_ID}),
        ),
        "unrelated_tenant_a_clinician": ActorContext(
            actor_id="PRAC-SYN-DERM-99",
            tenant_id=TENANT_A_ID,
            role="PHYSICIAN",
            specialty="Dermatology",
            care_patient_ids=frozenset({"PT-SYN-9999"}),
        ),
    }


def build_active_consent(
    *,
    consent_version_id: str = "CONSENT-2026-0131-V1",
    allowed_categories: frozenset[str] = DEFAULT_CLINICAL_CATEGORIES,
    allowed_purposes: frozenset[str] = frozenset({"clinical_care", "prior_auth_documentation"}),
    updated_at: datetime = JAN_31_NOW,
) -> ConsentRecord:
    """Return an active synthetic patient consent record for PT-SYN-0001."""
    return ConsentRecord(
        consent_version_id=consent_version_id,
        tenant_id=TENANT_A_ID,
        patient_id=PATIENT_ID,
        status=ConsentStatus.ACTIVE,
        allowed_categories=allowed_categories,
        allowed_purposes=allowed_purposes,
        updated_at=updated_at,
    )


def build_revoked_consent(
    *,
    consent_version_id: str = "CONSENT-2026-0331-REVOKED",
    updated_at: datetime = MAR_31_NOW,
) -> ConsentRecord:
    """Return a revoked synthetic patient consent record for PT-SYN-0001."""
    return ConsentRecord(
        consent_version_id=consent_version_id,
        tenant_id=TENANT_A_ID,
        patient_id=PATIENT_ID,
        status=ConsentStatus.REVOKED,
        allowed_categories=frozenset(),
        allowed_purposes=frozenset(),
        updated_at=updated_at,
    )


def build_jan31_neurology_transcript_segments() -> list[TranscriptSegment]:
    """Return verbatim speaker-diarized transcript segments for Jan 31 Neurology visit."""
    return [
        TranscriptSegment(
            evidence_id="SEG-001",
            speaker_label="SPEAKER_1",
            start_ms=0,
            end_ms=14500,
            text=(
                "My lower back pain has been radiating down my left leg for four weeks, "
                "and I have functional limits sitting longer than 20 minutes or climbing stairs."
            ),
        ),
        TranscriptSegment(
            evidence_id="SEG-002",
            speaker_label="SPEAKER_1",
            start_ms=15000,
            end_ms=26000,
            text=(
                "I stopped ibuprofen 400mg two weeks ago because it upset my stomach, "
                "and I deny bowel or bladder issues."
            ),
        ),
        TranscriptSegment(
            evidence_id="SEG-003",
            speaker_label="SPEAKER_0",
            start_ms=27000,
            end_ms=39000,
            text=(
                "On exam, straight leg raise is positive on the left at 40 degrees; "
                "motor strength is 5/5 bilaterally."
            ),
        ),
        TranscriptSegment(
            evidence_id="SEG-004",
            speaker_label="SPEAKER_0",
            start_ms=40000,
            end_ms=52000,
            text=(
                "We could consider gabapentin 300mg if MRI shows compression; "
                "I am not prescribing gabapentin today."
            ),
        ),
        TranscriptSegment(
            evidence_id="SEG-005",
            speaker_label="SPEAKER_0",
            start_ms=53000,
            end_ms=66000,
            text=(
                "Continue gentle daily walking as tolerated and maintain an anti-inflammatory diet; "
                "we will order a lumbar spine MRI and follow up after MRI results."
            ),
        ),
    ]


def store_jan31_transcript(
    artifact_store: InMemoryArtifactStore,
    *,
    tenant_id: str = TENANT_A_ID,
    segments: list[TranscriptSegment] | None = None,
) -> tuple[str, str]:
    """Persist Jan 31 verbatim transcript segments in private tenant GCS storage."""
    segs = segments if segments is not None else build_jan31_neurology_transcript_segments()
    object_path = build_tenant_artifact_path(
        tenant_id,
        "transcripts",
        f"{JAN_31_ENCOUNTER_ID}.json",
    )
    payload = json.dumps({"segments": [s.model_dump(mode="json") for s in segs]}).encode("utf-8")
    stored = artifact_store.put_artifact(
        tenant_id=tenant_id,
        object_path=object_path,
        data=payload,
        content_type="application/json",
    )
    return object_path, stored.version_id


def build_jan31_neurology_soap_draft(
    *,
    transcript_version_id: str = "1",
    draft_version: int = 1,
    draft_id: str = "DRAFT-2026-0131-NEURO",
    gabapentin_status: MedicationStatus = MedicationStatus.DISCUSSED,
    extra_assessment_note: str | None = None,
) -> SoapDraftPayload:
    """Return a schema-valid, evidence-grounded Jan 31 Neurology SOAP draft."""
    assessment_items = [
        SoapItem(
            item_id="ASSESS-01",
            category="assessment",
            statement="Left-sided lumbar radiculopathy with positive straight leg raise at 40 degrees.",
            polarity="PRESENT",
            certainty="suspected",
            evidence_ids=["SEG-001", "SEG-003"],
        ),
        SoapItem(
            item_id="ASSESS-02",
            category="red_flags",
            statement="Bowel or bladder dysfunction (cauda equina red flags).",
            polarity="NEGATED",
            certainty="ruled_out_by_history",
            evidence_ids=["SEG-002"],
        ),
    ]
    if extra_assessment_note:
        assessment_items.append(
            SoapItem(
                item_id="ASSESS-03",
                category="assessment",
                statement=extra_assessment_note,
                polarity="PRESENT",
                evidence_ids=["SEG-003"],
            )
        )

    return SoapDraftPayload(
        draft_id=draft_id,
        draft_version=draft_version,
        status="DRAFT_AWAITING_CLINICIAN_REVIEW",
        encounter_ref=EncounterRef(
            encounter_id=JAN_31_ENCOUNTER_ID,
            encounter_date=JAN_31_DATE,
            specialty="Neurology",
        ),
        source_refs=SourceRefs(
            transcript_version_id=transcript_version_id,
            fhir_prior_refs=[],
        ),
        speaker_attribution=SpeakerAttribution(
            status="UNVERIFIED_REQUIRES_CLINICIAN_CONFIRMATION",
            candidates=[
                SpeakerRoleCandidate(diarization_label="SPEAKER_0", proposed_role="CLINICIAN"),
                SpeakerRoleCandidate(diarization_label="SPEAKER_1", proposed_role="PATIENT"),
            ],
        ),
        soap=SoapSections(
            subjective=[
                SoapItem(
                    item_id="SUBJ-01",
                    category="chief_complaint",
                    statement=(
                        "Lower back pain radiating to left leg for 4 weeks with functional limits "
                        "sitting longer than 20 minutes and climbing stairs."
                    ),
                    polarity="PRESENT",
                    speaker_role="PATIENT",
                    evidence_ids=["SEG-001"],
                ),
                SoapItem(
                    item_id="SUBJ-02",
                    category="review_of_systems",
                    statement="Denies bowel or bladder issues.",
                    polarity="NEGATED",
                    speaker_role="PATIENT",
                    evidence_ids=["SEG-002"],
                ),
            ],
            objective=[
                SoapItem(
                    item_id="OBJ-01",
                    category="physical_exam",
                    statement=(
                        "Straight leg raise (SLR) positive on the left at 40 degrees; "
                        "bilateral lower extremity motor strength 5/5."
                    ),
                    polarity="PRESENT",
                    speaker_role="CLINICIAN",
                    evidence_ids=["SEG-003"],
                ),
            ],
            assessment=assessment_items,
            plan=SoapPlan(
                medications=[
                    MedicationItem(
                        item_id="MED-01",
                        medication_name="gabapentin",
                        dose=300,
                        unit="mg",
                        route=None,
                        frequency=None,
                        missing_reason="Discussed conditionally pending MRI; route and frequency not ordered.",
                        event_date=JAN_31_DATE,
                        medication_status=gabapentin_status,
                        status_rationale=(
                            "Clinician noted gabapentin 300mg could be considered if MRI shows "
                            "compression; explicitly not prescribing today."
                        ),
                        evidence_ids=["SEG-004"],
                    ),
                    MedicationItem(
                        item_id="MED-02",
                        medication_name="ibuprofen",
                        dose=400,
                        unit="mg",
                        route="oral",
                        frequency=None,
                        missing_reason="Patient stopped 2 weeks ago; prior frequency not stated.",
                        event_date=JAN_31_DATE,
                        medication_status=MedicationStatus.STOPPED,
                        status_rationale="Patient reported stopping ibuprofen 400mg two weeks ago.",
                        evidence_ids=["SEG-002"],
                    ),
                ],
                lifestyle_and_activity=[
                    SoapItem(
                        item_id="LIFE-01",
                        category="lifestyle",
                        statement="Gentle daily walking as tolerated and anti-inflammatory diet.",
                        polarity="PRESENT",
                        speaker_role="CLINICIAN",
                        evidence_ids=["SEG-005"],
                    ),
                ],
                follow_up=[
                    SoapItem(
                        item_id="FU-01",
                        category="follow_up",
                        statement="Lumbar spine MRI order and follow-up review after MRI results.",
                        polarity="PRESENT",
                        speaker_role="CLINICIAN",
                        evidence_ids=["SEG-005"],
                    ),
                ],
            ),
        ),
        review_flags=[
            ReviewFlag(
                flag_id="FLAG-01",
                severity="INFO",
                code="MEDICATION_DISCUSSED_ONLY",
                message="Gabapentin 300mg was discussed conditionally and not prescribed.",
            )
        ],
    )


def build_mar31_orthopedics_context() -> CurrentEncounterContext:
    """Return the Mar 31 Orthopedics encounter context kept distinct from Jan 31 history."""
    return CurrentEncounterContext(
        encounter_id=MAR_31_ENCOUNTER_ID,
        encounter_date=MAR_31_DATE,
        specialty="Orthopedics",
        assessment_notes=[
            "Mar 31 Orthopedics consultation for persistent left lumbar radiculopathy.",
        ],
    )


def seed_base_fhir_entities(fhir_store: InMemoryFhirR4Store, tenant_id: str = TENANT_A_ID) -> None:
    """Seed Patient PT-SYN-0001, Organization, Practitioners, Roles, and Jan 31 / Mar 31 Encounters."""
    fhir_store.create(
        tenant_id,
        {
            "resourceType": "Patient",
            "id": PATIENT_ID,
            "name": [{"text": PATIENT_DISPLAY_NAME}],
            "birthDate": PATIENT_SYNTHETIC_DOB,
            "identifier": [{"system": "urn:carethread:mrn", "value": PATIENT_SYNTHETIC_MRN}],
        },
    )
    fhir_store.create(
        tenant_id,
        {
            "resourceType": "Organization",
            "id": ORG_SYN_A_ID,
            "name": ORG_SYN_A_NAME,
        },
    )
    fhir_store.create(
        tenant_id,
        {
            "resourceType": "Practitioner",
            "id": DR_MAYA_LIN_ID,
            "name": [{"text": DR_MAYA_LIN_NAME}],
        },
    )
    fhir_store.create(
        tenant_id,
        {
            "resourceType": "Practitioner",
            "id": DR_DAVID_OKAFOR_ID,
            "name": [{"text": DR_DAVID_OKAFOR_NAME}],
        },
    )
    fhir_store.create(
        tenant_id,
        {
            "resourceType": "PractitionerRole",
            "id": "ROLE-SYN-NEURO-01",
            "practitioner": {"reference": f"Practitioner/{DR_MAYA_LIN_ID}"},
            "organization": {"reference": f"Organization/{ORG_SYN_A_ID}"},
            "specialty": [{"text": "Neurology"}],
        },
    )
    fhir_store.create(
        tenant_id,
        {
            "resourceType": "PractitionerRole",
            "id": "ROLE-SYN-ORTHO-01",
            "practitioner": {"reference": f"Practitioner/{DR_DAVID_OKAFOR_ID}"},
            "organization": {"reference": f"Organization/{ORG_SYN_A_ID}"},
            "specialty": [{"text": "Orthopedics"}],
        },
    )
    fhir_store.create(
        tenant_id,
        {
            "resourceType": "Encounter",
            "id": JAN_31_ENCOUNTER_ID,
            "status": "finished",
            "subject": {"reference": f"Patient/{PATIENT_ID}"},
            "serviceType": {"text": "Neurology"},
            "period": {"start": "2026-01-31T15:00:00Z", "end": "2026-01-31T15:30:00Z"},
            "participant": [{"individual": {"reference": f"Practitioner/{DR_MAYA_LIN_ID}"}}],
            "serviceProvider": {"reference": f"Organization/{ORG_SYN_A_ID}"},
        },
    )
    fhir_store.create(
        tenant_id,
        {
            "resourceType": "Encounter",
            "id": MAR_31_ENCOUNTER_ID,
            "status": "in-progress",
            "subject": {"reference": f"Patient/{PATIENT_ID}"},
            "serviceType": {"text": "Orthopedics"},
            "period": {"start": "2026-03-31T14:00:00Z"},
            "participant": [{"individual": {"reference": f"Practitioner/{DR_DAVID_OKAFOR_ID}"}}],
            "serviceProvider": {"reference": f"Organization/{ORG_SYN_A_ID}"},
        },
    )


def seed_jan31_signed_fhir_and_graph(
    runtime: EvalRuntime,
    *,
    idempotency_key: str = "commit-ENC-2026-0131-NEURO-v1",
) -> FhirCommitResult:
    """Seed base FHIR entities, commit signed Jan 31 Neurology SOAP, and rebuild Spanner Graph."""
    seed_base_fhir_entities(runtime.fhir_store, tenant_id=TENANT_A_ID)
    object_path, version_id = store_jan31_transcript(runtime.artifact_store, tenant_id=TENANT_A_ID)
    draft = build_jan31_neurology_soap_draft(transcript_version_id=version_id)
    commit_result = runtime.fhir_writer.commit_signed_soap(
        tenant_id=TENANT_A_ID,
        encounter_id=JAN_31_ENCOUNTER_ID,
        signed_soap=draft,
        signer_id=DR_MAYA_LIN_ID,
        transcript_ref=f"gs://carethread-private-artifacts/{object_path}#{version_id}",
        idempotency_key=idempotency_key,
    )
    runtime.graph_store.rebuild_from_fhir(TENANT_A_ID, runtime.fhir_store)
    return commit_result
