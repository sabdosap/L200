"""Draft and FHIR commit job state machine contracts (v1.0.0)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field

from carethread.schemas import StrictModel


class JobState(str, Enum):
    """Canonical lifecycle states for clinical transcription, drafting, and FHIR commit jobs."""

    QUEUED = "QUEUED"
    TRANSCRIBING = "TRANSCRIBING"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    TRANSCRIBED = "TRANSCRIBED"
    DRAFTING = "DRAFTING"
    AWAITING_CLINICIAN_REVIEW = "AWAITING_CLINICIAN_REVIEW"
    REJECTED_BY_CLINICIAN = "REJECTED_BY_CLINICIAN"
    COMMITTING_FHIR = "COMMITTING_FHIR"
    RECONCILING_FHIR = "RECONCILING_FHIR"
    SIGNED_COMMITTED = "SIGNED_COMMITTED"
    FAILED_TERMINAL = "FAILED_TERMINAL"


VALID_JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.TRANSCRIBING, JobState.FAILED_TERMINAL}),
    JobState.TRANSCRIBING: frozenset({
        JobState.TRANSCRIBED,
        JobState.FAILED_RETRYABLE,
        JobState.FAILED_TERMINAL,
    }),
    JobState.FAILED_RETRYABLE: frozenset({JobState.TRANSCRIBING, JobState.FAILED_TERMINAL}),
    JobState.TRANSCRIBED: frozenset({JobState.DRAFTING, JobState.FAILED_TERMINAL}),
    JobState.DRAFTING: frozenset({JobState.AWAITING_CLINICIAN_REVIEW, JobState.FAILED_TERMINAL}),
    JobState.AWAITING_CLINICIAN_REVIEW: frozenset({
        JobState.REJECTED_BY_CLINICIAN,
        JobState.COMMITTING_FHIR,
        JobState.FAILED_TERMINAL,
    }),
    JobState.REJECTED_BY_CLINICIAN: frozenset({JobState.DRAFTING, JobState.FAILED_TERMINAL}),
    JobState.COMMITTING_FHIR: frozenset({
        JobState.SIGNED_COMMITTED,
        JobState.RECONCILING_FHIR,
        JobState.FAILED_TERMINAL,
    }),
    JobState.RECONCILING_FHIR: frozenset({
        JobState.SIGNED_COMMITTED,
        JobState.COMMITTING_FHIR,
        JobState.FAILED_TERMINAL,
    }),
    JobState.SIGNED_COMMITTED: frozenset(),
    JobState.FAILED_TERMINAL: frozenset(),
}


class JobRecord(StrictModel):
    """Versioned durable job metadata record (v1.0.0)."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    job_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    encounter_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    state: JobState
    attempt_count: int = Field(default=0, ge=0)
    regeneration_count: int = Field(default=0, ge=0)
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    updated_at: datetime


__all__ = [
    "JobRecord",
    "JobState",
    "VALID_JOB_TRANSITIONS",
]
