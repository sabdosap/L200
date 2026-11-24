"""Firestore-style job state machine, 90s leases, atomic outbox, and FHIR reconciliation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal
import uuid

from pydantic import Field

from carethread.errors import ErrorCategory, GuidedErrorEnvelope, GuidedErrorException
from carethread.schemas import StrictModel
from carethread.schemas.v1.jobs import VALID_JOB_TRANSITIONS, JobRecord, JobState
from carethread.schemas.v1.soap import SoapDraftPayload
from carethread.stores.fhir import (
    IDEMPOTENCY_SYSTEM,
    DeterministicFhirWriter,
    FhirCommitResult,
    FhirR4Store,
)

DEFAULT_LEASE_SECONDS = 90
GRAPH_REFRESH_PENDING = "GRAPH_REFRESH_PENDING"


class OutboxEvent(StrictModel):
    """Durable outbox event enqueued atomically with a job state transition."""

    event_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    encounter_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["PENDING", "DISPATCHED", "COMPLETED"] = "PENDING"
    created_at: datetime


class JobOutboxStore:
    """In-memory Firestore job state machine with 90s leases and atomic outbox."""

    def __init__(self, default_lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        self._default_lease_seconds = default_lease_seconds
        self._jobs: dict[str, JobRecord] = {}
        self._idempotency_index: dict[tuple[str, str], str] = {}
        self._outbox_events: dict[str, OutboxEvent] = {}
        self._outbox_idempotency_index: dict[tuple[str, str], str] = {}

    def _now(self, now: datetime | None) -> datetime:
        return now if now is not None else datetime.now(timezone.utc)

    def create_or_get_job(
        self,
        tenant_id: str,
        encounter_id: str,
        idempotency_key: str,
        initial_state: JobState = JobState.QUEUED,
        job_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[JobRecord, bool]:
        """Create a job or absorb duplicate delivery by returning the existing record."""
        key = (tenant_id, idempotency_key)
        existing_id = self._idempotency_index.get(key)
        if existing_id is not None:
            return self._jobs[existing_id].model_copy(deep=True), False

        ts = self._now(now)
        resolved_job_id = job_id or f"job-{uuid.uuid4().hex}"
        record = JobRecord(
            job_id=resolved_job_id,
            tenant_id=tenant_id,
            encounter_id=encounter_id,
            idempotency_key=idempotency_key,
            state=initial_state,
            attempt_count=0,
            regeneration_count=0,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=ts,
        )
        self._jobs[resolved_job_id] = record
        self._idempotency_index[key] = resolved_job_id
        return record.model_copy(deep=True), True

    def get_job(self, job_id: str) -> JobRecord:
        """Fetch a job record by job_id."""
        record = self._jobs.get(job_id)
        if record is None:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="JOB_NOT_FOUND",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="job_id",
                    guided_recovery="Verify the job_id exists in JobOutboxStore.",
                    trace_id=uuid.uuid4().hex,
                )
            )
        return record.model_copy(deep=True)

    def get_job_by_idempotency_key(
        self,
        tenant_id: str,
        idempotency_key: str,
    ) -> JobRecord | None:
        """Look up a job by (tenant_id, idempotency_key)."""
        job_id = self._idempotency_index.get((tenant_id, idempotency_key))
        if job_id is None:
            return None
        return self._jobs[job_id].model_copy(deep=True)

    def acquire_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: int | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        """Acquire or renew a 90s exclusive worker lease for a job."""
        ts = self._now(now)
        record = self._jobs.get(job_id)
        if record is None:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="JOB_NOT_FOUND",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="job_id",
                    guided_recovery="Verify the job_id exists before acquiring a lease.",
                    trace_id=uuid.uuid4().hex,
                )
            )

        if (
            record.lease_owner is not None
            and record.lease_owner != worker_id
            and record.lease_expires_at is not None
            and record.lease_expires_at > ts
        ):
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="JOB_LEASE_CONFLICT",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=True,
                    max_retries_remaining=3,
                    field_path="lease_owner",
                    guided_recovery="Wait until the active 90s worker lease expires before retrying.",
                    trace_id=uuid.uuid4().hex,
                )
            )

        ttl = self._default_lease_seconds if lease_seconds is None else lease_seconds
        updated = record.model_copy(
            update={
                "lease_owner": worker_id,
                "lease_expires_at": ts + timedelta(seconds=ttl),
                "updated_at": ts,
            }
        )
        self._jobs[job_id] = updated
        return updated.model_copy(deep=True)

    def release_lease(
        self,
        job_id: str,
        worker_id: str,
        now: datetime | None = None,
    ) -> JobRecord:
        """Release a worker lease if held by worker_id."""
        ts = self._now(now)
        record = self.get_job(job_id)
        if record.lease_owner is not None and record.lease_owner != worker_id:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="JOB_LEASE_NOT_OWNED",
                    category=ErrorCategory.AUTHZ_DENIED,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="lease_owner",
                    guided_recovery="Only the current lease owner may release the lease.",
                    trace_id=uuid.uuid4().hex,
                )
            )
        updated = record.model_copy(
            update={
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": ts,
            }
        )
        self._jobs[job_id] = updated
        return updated.model_copy(deep=True)

    def transition_job(
        self,
        job_id: str,
        to_state: JobState,
        worker_id: str | None = None,
        now: datetime | None = None,
        outbox_events: list[OutboxEvent] | None = None,
        increment_attempt: bool = False,
        increment_regeneration: bool = False,
    ) -> JobRecord:
        """Atomically validate state transition, check lease, and enqueue outbox events."""
        ts = self._now(now)
        record = self._jobs.get(job_id)
        if record is None:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="JOB_NOT_FOUND",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="job_id",
                    guided_recovery="Verify the job_id exists before transitioning state.",
                    trace_id=uuid.uuid4().hex,
                )
            )

        if (
            record.lease_owner is not None
            and record.lease_expires_at is not None
            and record.lease_expires_at > ts
            and worker_id != record.lease_owner
        ):
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="JOB_LEASE_CONFLICT",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=True,
                    max_retries_remaining=2,
                    field_path="lease_owner",
                    guided_recovery="Acquire a valid lease before mutating job state.",
                    trace_id=uuid.uuid4().hex,
                )
            )

        allowed = VALID_JOB_TRANSITIONS[record.state]
        if to_state not in allowed:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="INVALID_JOB_STATE_TRANSITION",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="state",
                    guided_recovery=(
                        f"Transition from {record.state.value} to {to_state.value} is not allowed."
                    ),
                    trace_id=uuid.uuid4().hex,
                )
            )

        updated = record.model_copy(
            update={
                "state": to_state,
                "attempt_count": record.attempt_count + (1 if increment_attempt else 0),
                "regeneration_count": record.regeneration_count
                + (1 if increment_regeneration else 0),
                "updated_at": ts,
            }
        )
        self._jobs[job_id] = updated

        for event in outbox_events or []:
            dedup_key = (event.tenant_id, event.idempotency_key)
            if dedup_key not in self._outbox_idempotency_index:
                self._outbox_events[event.event_id] = event.model_copy(deep=True)
                self._outbox_idempotency_index[dedup_key] = event.event_id

        return updated.model_copy(deep=True)

    def list_outbox_events(
        self,
        tenant_id: str | None = None,
        event_type: str | None = None,
        status: str | None = None,
    ) -> list[OutboxEvent]:
        """List outbox events filtered by optional tenant, event_type, and status."""
        results: list[OutboxEvent] = []
        for event in self._outbox_events.values():
            if tenant_id is not None and event.tenant_id != tenant_id:
                continue
            if event_type is not None and event.event_type != event_type:
                continue
            if status is not None and event.status != status:
                continue
            results.append(event.model_copy(deep=True))
        return results

    def mark_outbox_event_dispatched(self, event_id: str) -> OutboxEvent:
        """Mark a pending outbox event as DISPATCHED."""
        event = self._outbox_events.get(event_id)
        if event is None:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="OUTBOX_EVENT_NOT_FOUND",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="event_id",
                    guided_recovery="Verify the outbox event_id exists.",
                    trace_id=uuid.uuid4().hex,
                )
            )
        updated = event.model_copy(update={"status": "DISPATCHED"})
        self._outbox_events[event_id] = updated
        return updated.model_copy(deep=True)


class FhirCommitReconciler:
    """Coordinates lease-protected FHIR commits and RECONCILING_FHIR lost-response recovery."""

    def __init__(
        self,
        outbox_store: JobOutboxStore,
        fhir_store: FhirR4Store,
        writer: DeterministicFhirWriter,
    ) -> None:
        self._outbox_store = outbox_store
        self._fhir_store = fhir_store
        self._writer = writer

    def _build_graph_refresh_event(
        self,
        job: JobRecord,
        commit_result: FhirCommitResult,
        now: datetime,
    ) -> OutboxEvent:
        return OutboxEvent(
            event_id=f"outbox-{uuid.uuid4().hex}",
            tenant_id=job.tenant_id,
            job_id=job.job_id,
            encounter_id=job.encounter_id,
            event_type=GRAPH_REFRESH_PENDING,
            idempotency_key=f"{job.idempotency_key}:{GRAPH_REFRESH_PENDING}",
            payload={
                "composition_ref": commit_result.composition_ref,
                "provenance_ref": commit_result.provenance_ref,
                "resource_refs": list(commit_result.resource_refs),
            },
            created_at=now,
        )

    def _lookup_existing_commit(self, job: JobRecord) -> FhirCommitResult | None:
        bundle = self._fhir_store.search(
            tenant_id=job.tenant_id,
            resource_type="Composition",
            identifier=f"{IDEMPOTENCY_SYSTEM}|{job.idempotency_key}",
            revinclude="Provenance:target",
        )
        composition: dict[str, Any] | None = None
        provenance: dict[str, Any] | None = None
        for entry in bundle.get("entry") or []:
            res = entry.get("resource") or {}
            if res.get("resourceType") == "Composition" and composition is None:
                composition = res
            elif res.get("resourceType") == "Provenance" and provenance is None:
                provenance = res

        if composition is None or provenance is None:
            return None

        comp_ref = f"Composition/{composition['id']}/_history/{composition['meta']['versionId']}"
        prov_ref = f"Provenance/{provenance['id']}/_history/{provenance['meta']['versionId']}"
        target_refs = [
            t["reference"]
            for t in (provenance.get("target") or [])
            if isinstance(t, dict) and isinstance(t.get("reference"), str)
        ]
        all_refs = list(dict.fromkeys([*target_refs, prov_ref]))

        return FhirCommitResult(
            tenant_id=job.tenant_id,
            encounter_id=job.encounter_id,
            idempotency_key=job.idempotency_key,
            composition_ref=comp_ref,
            provenance_ref=prov_ref,
            resource_refs=all_refs,
            created_new=False,
        )

    def commit_signed_soap_job(
        self,
        job_id: str,
        worker_id: str,
        signed_soap: SoapDraftPayload,
        signer_id: str,
        transcript_ref: str,
        now: datetime | None = None,
    ) -> FhirCommitResult:
        """Execute a lease-protected signed SOAP commit, transitioning to RECONCILING_FHIR on timeout."""
        ts = now if now is not None else datetime.now(timezone.utc)
        job = self._outbox_store.acquire_lease(job_id, worker_id=worker_id, now=ts)

        if job.state == JobState.SIGNED_COMMITTED:
            existing = self._lookup_existing_commit(job)
            if existing is not None:
                return existing

        if job.state == JobState.RECONCILING_FHIR:
            recovered = self.reconcile_job(
                job_id=job_id,
                worker_id=worker_id,
                signed_soap=signed_soap,
                signer_id=signer_id,
                transcript_ref=transcript_ref,
                now=ts,
            )
            if recovered is not None:
                return recovered
            job = self._outbox_store.get_job(job_id)

        if job.state == JobState.AWAITING_CLINICIAN_REVIEW:
            job = self._outbox_store.transition_job(
                job_id=job_id,
                to_state=JobState.COMMITTING_FHIR,
                worker_id=worker_id,
                now=ts,
                increment_attempt=True,
            )

        try:
            result = self._writer.commit_signed_soap(
                tenant_id=job.tenant_id,
                encounter_id=job.encounter_id,
                signed_soap=signed_soap,
                signer_id=signer_id,
                transcript_ref=transcript_ref,
                idempotency_key=job.idempotency_key,
            )
        except TimeoutError:
            self._outbox_store.transition_job(
                job_id=job_id,
                to_state=JobState.RECONCILING_FHIR,
                worker_id=worker_id,
                now=ts,
            )
            raise

        event = self._build_graph_refresh_event(job, result, ts)
        self._outbox_store.transition_job(
            job_id=job_id,
            to_state=JobState.SIGNED_COMMITTED,
            worker_id=worker_id,
            now=ts,
            outbox_events=[event],
        )
        return result

    def reconcile_job(
        self,
        job_id: str,
        worker_id: str,
        signed_soap: SoapDraftPayload | None = None,
        signer_id: str | None = None,
        transcript_ref: str | None = None,
        now: datetime | None = None,
    ) -> FhirCommitResult | None:
        """Reconcile a job in RECONCILING_FHIR by querying FHIR R4 by idempotency key."""
        ts = now if now is not None else datetime.now(timezone.utc)
        job = self._outbox_store.acquire_lease(job_id, worker_id=worker_id, now=ts)

        if job.state == JobState.SIGNED_COMMITTED:
            return self._lookup_existing_commit(job)

        if job.state != JobState.RECONCILING_FHIR:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="JOB_NOT_RECONCILING",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="state",
                    guided_recovery="Only jobs in RECONCILING_FHIR can be reconciled.",
                    trace_id=uuid.uuid4().hex,
                )
            )

        existing = self._lookup_existing_commit(job)
        if existing is not None:
            event = self._build_graph_refresh_event(job, existing, ts)
            self._outbox_store.transition_job(
                job_id=job_id,
                to_state=JobState.SIGNED_COMMITTED,
                worker_id=worker_id,
                now=ts,
                outbox_events=[event],
            )
            return existing

        # Commit never reached FHIR; transition back to COMMITTING_FHIR for idempotent retry.
        self._outbox_store.transition_job(
            job_id=job_id,
            to_state=JobState.COMMITTING_FHIR,
            worker_id=worker_id,
            now=ts,
            increment_attempt=True,
        )
        if signed_soap is not None and signer_id is not None and transcript_ref is not None:
            return self.commit_signed_soap_job(
                job_id=job_id,
                worker_id=worker_id,
                signed_soap=signed_soap,
                signer_id=signer_id,
                transcript_ref=transcript_ref,
                now=ts,
            )
        return None


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "GRAPH_REFRESH_PENDING",
    "FhirCommitReconciler",
    "JobOutboxStore",
    "OutboxEvent",
]
