"""Tests for private GCS store, authoritative FHIR R4 store, deterministic writer, and job outbox."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from carethread.errors import ErrorCategory, GuidedErrorException
from carethread.schemas.v1.jobs import JobState
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
    SpeakerRoleCandidate,
)
from carethread.stores.fhir import (
    IDEMPOTENCY_SYSTEM,
    DeterministicFhirWriter,
    FhirPreconditionFailedError,
    InMemoryFhirR4Store,
)
from carethread.stores.gcs import InMemoryArtifactStore, build_tenant_artifact_path
from carethread.stores.outbox import FhirCommitReconciler, JobOutboxStore


def _make_jan31_soap() -> SoapDraftPayload:
    """Build the Jan 31 SOAP fixture with gabapentin (discussed) and ibuprofen (stopped)."""
    return SoapDraftPayload(
        draft_id="draft-jan31-001",
        draft_version=1,
        encounter_ref=EncounterRef(
            encounter_id="enc-2026-01-31",
            encounter_date=date(2026, 1, 31),
            specialty="primary-care",
        ),
        source_refs=SourceRefs(
            transcript_version_id="gs://carethread-private-artifacts/tenants/tenant-a/transcripts/enc-2026-01-31-v1.json",
            fhir_prior_refs=["Composition/comp-prior-1/_history/1"],
        ),
        speaker_attribution=SpeakerAttribution(
            status="CLINICIAN_CONFIRMED",
            candidates=[
                SpeakerRoleCandidate(diarization_label="SPEAKER_0", proposed_role="CLINICIAN"),
                SpeakerRoleCandidate(diarization_label="SPEAKER_1", proposed_role="PATIENT"),
            ],
        ),
        soap=SoapSections(
            subjective=[
                SoapItem(
                    item_id="subj-1",
                    category="chief-complaint",
                    statement="Persistent burning neuropathic foot pain worse at night.",
                    polarity="PRESENT",
                    speaker_role="PATIENT",
                    evidence_ids=["ev-001"],
                )
            ],
            objective=[
                SoapItem(
                    item_id="obj-1",
                    category="vital-sign",
                    statement="BP 128/78 mmHg; monofilament sensation mildly reduced bilaterally.",
                    polarity="PRESENT",
                    speaker_role="CLINICIAN",
                    evidence_ids=["ev-002"],
                )
            ],
            assessment=[
                SoapItem(
                    item_id="assess-1",
                    category="diagnosis",
                    statement="Painful peripheral neuropathy, likely diabetic etiology.",
                    polarity="PRESENT",
                    certainty="probable",
                    evidence_ids=["ev-001", "ev-002"],
                )
            ],
            plan=SoapPlan(
                medications=[
                    MedicationItem(
                        item_id="med-gabapentin",
                        medication_name="gabapentin",
                        dose=None,
                        unit=None,
                        route=None,
                        frequency=None,
                        missing_reason="Discussed as a future option only; not prescribed today.",
                        event_date=date(2026, 1, 31),
                        medication_status=MedicationStatus.DISCUSSED,
                        status_rationale="Clinician mentioned gabapentin as an option if symptoms persist.",
                        evidence_ids=["ev-003"],
                    ),
                    MedicationItem(
                        item_id="med-ibuprofen",
                        medication_name="ibuprofen",
                        dose=400,
                        unit="mg",
                        route="oral",
                        frequency="PRN",
                        event_date=date(2026, 1, 31),
                        medication_status=MedicationStatus.STOPPED,
                        status_rationale="Patient discontinued ibuprofen due to gastric upset.",
                        evidence_ids=["ev-004"],
                    ),
                ],
                lifestyle_and_activity=[
                    SoapItem(
                        item_id="life-1",
                        category="lifestyle",
                        statement="Daily foot inspection and supportive footwear.",
                        polarity="PRESENT",
                        evidence_ids=["ev-005"],
                    )
                ],
                follow_up=[
                    SoapItem(
                        item_id="fu-1",
                        category="follow-up",
                        statement="Follow up in 4 weeks with fasting HbA1c results.",
                        polarity="PRESENT",
                        evidence_ids=["ev-006"],
                    )
                ],
            ),
        ),
    )


def _make_prescribed_soap() -> SoapDraftPayload:
    """Build a SOAP fixture with an explicitly prescribed medication."""
    soap = _make_jan31_soap()
    soap.draft_id = "draft-prescribed-001"
    soap.soap.plan.medications = [
        MedicationItem(
            item_id="med-lisinopril",
            medication_name="lisinopril",
            dose=10,
            unit="mg",
            route="oral",
            frequency="once daily",
            event_date=date(2026, 2, 10),
            medication_status=MedicationStatus.PRESCRIBED,
            status_rationale="Clinician explicitly ordered lisinopril 10 mg daily.",
            evidence_ids=["ev-101"],
        )
    ]
    return soap


class TestDeterministicFhirWriterAndMedicationSafety:
    def test_jan31_discussed_and_stopped_meds_create_zero_medication_requests(self) -> None:
        store = InMemoryFhirR4Store()
        writer = DeterministicFhirWriter(store)
        soap = _make_jan31_soap()

        result = writer.commit_signed_soap(
            tenant_id="tenant-a",
            encounter_id="enc-2026-01-31",
            signed_soap=soap,
            signer_id="practitioner-42",
            transcript_ref=soap.source_refs.transcript_version_id,
            idempotency_key="idem-jan31-001",
        )

        assert result.created_new is True
        assert result.composition_ref.startswith("Composition/")
        assert "/_history/1" in result.composition_ref
        assert result.provenance_ref.startswith("Provenance/")
        assert "/_history/1" in result.provenance_ref

        compositions = store.list_resources("tenant-a", "Composition")
        conditions = store.list_resources("tenant-a", "Condition")
        observations = store.list_resources("tenant-a", "Observation")
        med_statements = store.list_resources("tenant-a", "MedicationStatement")
        med_requests = store.list_resources("tenant-a", "MedicationRequest")
        provenances = store.list_resources("tenant-a", "Provenance")

        assert len(compositions) == 1
        assert compositions[0]["status"] == "final"
        assert len(conditions) == 1
        assert len(observations) == 2
        assert len(med_statements) == 2
        assert len(med_requests) == 0
        assert len(provenances) == 1

        statuses_by_name = {
            stmt["medicationCodeableConcept"]["text"]: stmt["status"]
            for stmt in med_statements
        }
        assert statuses_by_name["gabapentin"] == "not-taken"
        assert statuses_by_name["ibuprofen"] == "stopped"
        assert all(stmt["status"] != "active" for stmt in med_statements)

        # Provenance targets must be versioned references (ResourceType/id/_history/versionId)
        target_refs = [t["reference"] for t in provenances[0]["target"]]
        assert result.composition_ref in target_refs
        assert all("/_history/" in ref for ref in target_refs)

    def test_all_non_prescribed_statuses_never_create_medication_request_or_active_statement(
        self,
    ) -> None:
        store = InMemoryFhirR4Store()
        writer = DeterministicFhirWriter(store)
        soap = _make_jan31_soap()
        non_prescribed = [
            MedicationStatus.DISCUSSED,
            MedicationStatus.PLANNED,
            MedicationStatus.REPORTED,
            MedicationStatus.STOPPED,
            MedicationStatus.UNCLEAR,
        ]
        soap.soap.plan.medications = [
            MedicationItem(
                item_id=f"med-{status.value}",
                medication_name=f"drug-{status.value}",
                dose=None,
                unit=None,
                route=None,
                frequency=None,
                missing_reason=f"Not prescribed ({status.value})",
                event_date=date(2026, 1, 31),
                medication_status=status,
                status_rationale=f"Status is {status.value}",
                evidence_ids=[f"ev-{status.value}"],
            )
            for status in non_prescribed
        ]

        writer.commit_signed_soap(
            tenant_id="tenant-a",
            encounter_id="enc-2026-01-31",
            signed_soap=soap,
            signer_id="practitioner-42",
            transcript_ref=soap.source_refs.transcript_version_id,
            idempotency_key="idem-non-prescribed",
        )

        assert len(store.list_resources("tenant-a", "MedicationRequest")) == 0
        stmts = store.list_resources("tenant-a", "MedicationStatement")
        assert len(stmts) == 5
        assert all(stmt["status"] in {"not-taken", "stopped", "unknown", "intended"} for stmt in stmts)

    def test_prescribed_medication_creates_medication_request_intent_order(self) -> None:
        store = InMemoryFhirR4Store()
        writer = DeterministicFhirWriter(store)
        soap = _make_prescribed_soap()

        writer.commit_signed_soap(
            tenant_id="tenant-a",
            encounter_id="enc-2026-02-10",
            signed_soap=soap,
            signer_id="practitioner-42",
            transcript_ref=soap.source_refs.transcript_version_id,
            idempotency_key="idem-prescribed-001",
        )

        med_requests = store.list_resources("tenant-a", "MedicationRequest")
        med_statements = store.list_resources("tenant-a", "MedicationStatement")
        assert len(med_requests) == 1
        assert med_requests[0]["intent"] == "order"
        assert med_requests[0]["status"] == "active"
        assert med_requests[0]["medicationCodeableConcept"]["text"] == "lisinopril"
        assert len(med_statements) == 0

    def test_duplicate_delivery_three_times_produces_single_composition_and_provenance(self) -> None:
        store = InMemoryFhirR4Store()
        writer = DeterministicFhirWriter(store)
        soap = _make_jan31_soap()

        results = [
            writer.commit_signed_soap(
                tenant_id="tenant-a",
                encounter_id="enc-2026-01-31",
                signed_soap=soap,
                signer_id="practitioner-42",
                transcript_ref=soap.source_refs.transcript_version_id,
                idempotency_key="idem-dup-3x",
            )
            for _ in range(3)
        ]

        assert results[0].created_new is True
        assert results[1].created_new is False
        assert results[2].created_new is False
        assert results[0].composition_ref == results[1].composition_ref == results[2].composition_ref
        assert results[0].provenance_ref == results[1].provenance_ref == results[2].provenance_ref

        assert len(store.list_resources("tenant-a", "Composition")) == 1
        assert len(store.list_resources("tenant-a", "Provenance")) == 1
        assert len(store.list_resources("tenant-a", "Condition")) == 1
        assert len(store.list_resources("tenant-a", "Observation")) == 2
        assert len(store.list_resources("tenant-a", "MedicationStatement")) == 2


class TestLostResponseReconciliationAndOutbox:
    def test_lost_response_enters_reconciling_and_recovers_with_zero_duplicates(self) -> None:
        fhir_store = InMemoryFhirR4Store(simulate_lost_response_once=True)
        writer = DeterministicFhirWriter(fhir_store)
        outbox_store = JobOutboxStore()
        reconciler = FhirCommitReconciler(
            outbox_store=outbox_store,
            fhir_store=fhir_store,
            writer=writer,
        )
        soap = _make_jan31_soap()
        now = datetime(2026, 1, 31, 15, 0, tzinfo=timezone.utc)

        job, created = outbox_store.create_or_get_job(
            tenant_id="tenant-a",
            encounter_id="enc-2026-01-31",
            idempotency_key="idem-lost-resp-001",
            initial_state=JobState.AWAITING_CLINICIAN_REVIEW,
            now=now,
        )
        assert created is True

        # First commit writes to FHIR, then raises TimeoutError (simulated lost response)
        with pytest.raises(TimeoutError):
            reconciler.commit_signed_soap_job(
                job_id=job.job_id,
                worker_id="worker-1",
                signed_soap=soap,
                signer_id="practitioner-42",
                transcript_ref=soap.source_refs.transcript_version_id,
                now=now,
            )

        # Job must have transitioned into RECONCILING_FHIR
        reconciling_job = outbox_store.get_job(job.job_id)
        assert reconciling_job.state == JobState.RECONCILING_FHIR
        assert len(outbox_store.list_outbox_events(tenant_id="tenant-a")) == 0

        # FHIR store already contains the committed Composition + Provenance
        assert len(fhir_store.list_resources("tenant-a", "Composition")) == 1
        assert len(fhir_store.list_resources("tenant-a", "Provenance")) == 1

        # Reconciler queries by urn:carethread:idempotency|<key> with _revinclude=Provenance:target
        recovered = reconciler.reconcile_job(
            job_id=job.job_id,
            worker_id="worker-1",
            now=now + timedelta(seconds=5),
        )
        assert recovered is not None
        assert recovered.created_new is False
        assert recovered.composition_ref.startswith("Composition/")
        assert recovered.provenance_ref.startswith("Provenance/")

        committed_job = outbox_store.get_job(job.job_id)
        assert committed_job.state == JobState.SIGNED_COMMITTED

        # Zero duplicate FHIR resources
        assert len(fhir_store.list_resources("tenant-a", "Composition")) == 1
        assert len(fhir_store.list_resources("tenant-a", "Provenance")) == 1
        assert len(fhir_store.list_resources("tenant-a", "MedicationRequest")) == 0

        # Exactly one GRAPH_REFRESH_PENDING outbox event enqueued
        events = outbox_store.list_outbox_events(
            tenant_id="tenant-a",
            event_type="GRAPH_REFRESH_PENDING",
        )
        assert len(events) == 1
        assert events[0].job_id == job.job_id

        # Duplicate delivery after SIGNED_COMMITTED is absorbed cleanly
        absorbed = reconciler.commit_signed_soap_job(
            job_id=job.job_id,
            worker_id="worker-1",
            signed_soap=soap,
            signer_id="practitioner-42",
            transcript_ref=soap.source_refs.transcript_version_id,
            now=now + timedelta(seconds=10),
        )
        assert absorbed.created_new is False
        assert len(fhir_store.list_resources("tenant-a", "Composition")) == 1
        assert len(outbox_store.list_outbox_events(tenant_id="tenant-a")) == 1

    def test_outbox_90s_lease_conflict_and_expiry_takeover(self) -> None:
        outbox_store = JobOutboxStore()
        t0 = datetime(2026, 1, 31, 15, 0, tzinfo=timezone.utc)
        job, _ = outbox_store.create_or_get_job(
            tenant_id="tenant-a",
            encounter_id="enc-1",
            idempotency_key="idem-lease-1",
            initial_state=JobState.QUEUED,
            now=t0,
        )

        leased = outbox_store.acquire_lease(job.job_id, worker_id="worker-a", now=t0)
        assert leased.lease_owner == "worker-a"
        assert leased.lease_expires_at == t0 + timedelta(seconds=90)

        # Another worker cannot steal an active lease at t0 + 30s
        with pytest.raises(GuidedErrorException):
            outbox_store.acquire_lease(
                job.job_id,
                worker_id="worker-b",
                now=t0 + timedelta(seconds=30),
            )

        # After 90s expiry, worker-b can take over the lease
        taken_over = outbox_store.acquire_lease(
            job.job_id,
            worker_id="worker-b",
            now=t0 + timedelta(seconds=91),
        )
        assert taken_over.lease_owner == "worker-b"

    def test_outbox_duplicate_job_delivery_absorption(self) -> None:
        outbox_store = JobOutboxStore()
        t0 = datetime(2026, 1, 31, 15, 0, tzinfo=timezone.utc)
        job1, created1 = outbox_store.create_or_get_job(
            tenant_id="tenant-a",
            encounter_id="enc-1",
            idempotency_key="idem-same-key",
            now=t0,
        )
        job2, created2 = outbox_store.create_or_get_job(
            tenant_id="tenant-a",
            encounter_id="enc-1",
            idempotency_key="idem-same-key",
            now=t0 + timedelta(seconds=2),
        )
        assert created1 is True
        assert created2 is False
        assert job1.job_id == job2.job_id


class TestFhirStoreVersioningAndOptimisticLocking:
    def test_vread_if_match_and_revinclude_provenance(self) -> None:
        store = InMemoryFhirR4Store()
        assert store.enable_resource_versioning is True

        comp, created = store.create(
            tenant_id="tenant-a",
            resource={
                "resourceType": "Composition",
                "status": "preliminary",
                "identifier": {"system": IDEMPOTENCY_SYSTEM, "value": "idem-v1"},
            },
            if_none_exist=f"identifier={IDEMPOTENCY_SYSTEM}|idem-v1",
        )
        assert created is True
        assert comp["meta"]["versionId"] == "1"

        # Optimistic update with matching If-Match succeeds
        updated_payload = dict(comp)
        updated_payload["status"] = "final"
        comp_v2 = store.update("tenant-a", updated_payload, if_match='W/"1"')
        assert comp_v2["meta"]["versionId"] == "2"

        # Stale If-Match fails with precondition error
        with pytest.raises(FhirPreconditionFailedError):
            store.update("tenant-a", updated_payload, if_match='W/"1"')

        # vread returns historical version 1 intact
        v1 = store.vread("tenant-a", "Composition", comp["id"], "1")
        v2 = store.vread("tenant-a", "Composition", comp["id"], "2")
        assert v1["status"] == "preliminary"
        assert v2["status"] == "final"

        # Provenance targeting versioned ref is returned by _revinclude=Provenance:target
        store.create(
            tenant_id="tenant-a",
            resource={
                "resourceType": "Provenance",
                "target": [{"reference": f"Composition/{comp['id']}/_history/2"}],
                "agent": [{"who": {"reference": "Practitioner/p1"}}],
            },
        )
        bundle = store.search(
            tenant_id="tenant-a",
            resource_type="Composition",
            identifier=f"{IDEMPOTENCY_SYSTEM}|idem-v1",
            revinclude="Provenance:target",
        )
        types = [entry["resource"]["resourceType"] for entry in bundle["entry"]]
        assert types == ["Composition", "Provenance"]


class TestPrivateArtifactStore:
    def test_enforces_tenant_prefix_and_cmek_metadata(self) -> None:
        gcs = InMemoryArtifactStore()
        valid_path = build_tenant_artifact_path("tenant-a", "transcripts", "enc-1-v1.json")
        stored = gcs.put_artifact(
            tenant_id="tenant-a",
            object_path=valid_path,
            data=b'{"segments": []}',
            content_type="application/json",
            cmek_key_name="projects/p/locations/us-central1/keyRings/kr/cryptoKeys/phi-key",
        )
        assert stored.tenant_id == "tenant-a"
        assert stored.object_path == "tenants/tenant-a/transcripts/enc-1-v1.json"
        assert stored.cmek_key_name.endswith("phi-key")
        assert len(stored.sha256) == 64

        data, meta = gcs.get_artifact("tenant-a", valid_path)
        assert data == b'{"segments": []}'
        assert meta.version_id == "1"

        # Cross-tenant access must fail closed with AUTHZ_DENIED
        with pytest.raises(GuidedErrorException) as exc_info:
            gcs.get_artifact("tenant-b", valid_path)
        assert exc_info.value.envelope.category == ErrorCategory.AUTHZ_DENIED

        # Writing outside tenant prefix must fail closed
        with pytest.raises(GuidedErrorException) as exc_info:
            gcs.put_artifact(
                tenant_id="tenant-a",
                object_path="tenants/tenant-b/audio/clip.wav",
                data=b"audio",
                content_type="audio/wav",
            )
        assert exc_info.value.envelope.category == ErrorCategory.AUTHZ_DENIED

        # Missing CMEK when store has no default must fail closed
        strict_gcs = InMemoryArtifactStore(default_cmek_key_name=None)
        with pytest.raises(GuidedErrorException):
            strict_gcs.put_artifact(
                tenant_id="tenant-a",
                object_path=valid_path,
                data=b"audio",
                content_type="audio/wav",
                cmek_key_name="",
            )
