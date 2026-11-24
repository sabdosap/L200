"""SOAP draft, transcript segment, medication status, and canonical hashing contracts (v1.0.0)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from carethread.schemas import StrictModel


def compute_canonical_sha256(model: BaseModel | Mapping[str, Any]) -> str:
    """Compute a deterministic lowercase SHA-256 hex digest using sorted-key canonical JSON."""
    payload = model.model_dump(mode="json") if isinstance(model, BaseModel) else dict(model)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MedicationStatus(str, Enum):
    """Six-state medication semantic status; only 'prescribed' implies an active order."""

    DISCUSSED = "discussed"
    PLANNED = "planned"
    PRESCRIBED = "prescribed"
    REPORTED = "reported"
    STOPPED = "stopped"
    UNCLEAR = "unclear"


class TranscriptSegment(StrictModel):
    """Single speaker-diarized verbatim transcript segment."""

    evidence_id: str = Field(min_length=1)
    speaker_label: str = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str

    @model_validator(mode="after")
    def _validate_timestamps(self) -> TranscriptSegment:
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")
        return self


class SoapItem(StrictModel):
    """Evidence-grounded SOAP statement or explicit null with missing_reason."""

    item_id: str = Field(min_length=1)
    category: str | None = None
    statement: str | None = None
    missing_reason: str | None = None
    polarity: Literal["PRESENT", "NEGATED", "UNCERTAIN"] | None = None
    speaker_role: str | None = None
    status: str | None = None
    certainty: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_statement_grounding(self) -> SoapItem:
        if self.statement is None:
            if not self.missing_reason or not self.missing_reason.strip():
                raise ValueError("missing_reason is required when statement is None")
            return self

        if not self.statement.strip():
            raise ValueError("statement must be non-empty when provided, or None with missing_reason")
        if not self.evidence_ids:
            raise ValueError("evidence_ids must be non-empty when statement is not None")
        return self


class MedicationItem(StrictModel):
    """Medication entry enforcing strict status semantics and explicit null handling."""

    item_id: str = Field(min_length=1)
    medication_name: str = Field(min_length=1)
    dose: float | int | None = None
    unit: str | None = None
    route: str | None = None
    frequency: str | None = None
    missing_reason: str | None = None
    event_date: date
    medication_status: MedicationStatus
    status_rationale: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_missing_medication_fields(self) -> MedicationItem:
        if any(value is None for value in (self.dose, self.unit, self.route, self.frequency)):
            if not self.missing_reason or not self.missing_reason.strip():
                raise ValueError("missing_reason is required when dose, unit, route, or frequency is None")
        return self


class ReviewFlag(StrictModel):
    """Clinician review attention or required action marker."""

    flag_id: str = Field(min_length=1)
    severity: Literal["REQUIRED_ACTION", "CLINICAL_ATTENTION", "INFO"]
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class EncounterRef(StrictModel):
    """Encounter metadata anchor for a SOAP draft."""

    encounter_id: str = Field(min_length=1)
    encounter_date: date
    specialty: str = Field(min_length=1)


class SourceRefs(StrictModel):
    """Immutable references to transcript and prior signed FHIR inputs."""

    transcript_version_id: str = Field(min_length=1)
    fhir_prior_refs: list[str] = Field(default_factory=list)


class SpeakerRoleCandidate(StrictModel):
    """Proposed diarization label to clinical role mapping."""

    diarization_label: str = Field(min_length=1)
    proposed_role: Literal["CLINICIAN", "PATIENT", "CAREGIVER", "OTHER"]


class SpeakerAttribution(StrictModel):
    """Clinician-verifiable speaker role assignment status."""

    status: str = Field(default="UNVERIFIED_REQUIRES_CLINICIAN_CONFIRMATION", min_length=1)
    candidates: list[SpeakerRoleCandidate] = Field(default_factory=list)


class SoapPlan(StrictModel):
    """Structured Plan section of a SOAP note."""

    medications: list[MedicationItem] = Field(default_factory=list)
    lifestyle_and_activity: list[SoapItem] = Field(default_factory=list)
    follow_up: list[SoapItem] = Field(default_factory=list)


class SoapSections(StrictModel):
    """Complete Subjective, Objective, Assessment, and Plan structure."""

    subjective: list[SoapItem] = Field(default_factory=list)
    objective: list[SoapItem] = Field(default_factory=list)
    assessment: list[SoapItem] = Field(default_factory=list)
    plan: SoapPlan


class SoapDraftPayload(StrictModel):
    """Top-level versioned SOAP draft payload (v1.0.0)."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    draft_id: str = Field(min_length=1)
    draft_version: int = Field(ge=1)
    status: str = Field(default="DRAFT_AWAITING_CLINICIAN_REVIEW", min_length=1)
    encounter_ref: EncounterRef
    source_refs: SourceRefs
    speaker_attribution: SpeakerAttribution
    soap: SoapSections
    review_flags: list[ReviewFlag] = Field(default_factory=list)


__all__ = [
    "EncounterRef",
    "MedicationItem",
    "MedicationStatus",
    "ReviewFlag",
    "SoapDraftPayload",
    "SoapItem",
    "SoapPlan",
    "SoapSections",
    "SourceRefs",
    "SpeakerAttribution",
    "SpeakerRoleCandidate",
    "TranscriptSegment",
    "compute_canonical_sha256",
]
