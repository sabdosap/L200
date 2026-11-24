"""Longitudinal continuity brief data contracts (v1.0.0)."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from carethread.schemas import StrictModel
from carethread.schemas.v1.soap import MedicationStatus


class ContinuitySource(str, Enum):
    """Provenance of retrieved longitudinal continuity data."""

    SPANNER_GRAPH_VERIFIED = "SPANNER_GRAPH_VERIFIED"
    FHIR_R4_AUTHORITATIVE = "FHIR_R4_AUTHORITATIVE"


class FhirVersionRef(StrictModel):
    """Version-pinned reference to an authoritative signed FHIR R4 resource."""

    resource_type: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)


class PriorHistoryItem(StrictModel):
    """Single dated, attributed, evidence-linked item from a prior signed encounter."""

    item_id: str = Field(min_length=1)
    encounter_id: str = Field(min_length=1)
    encounter_date: date
    specialty: str = Field(min_length=1)
    practitioner_name: str | None = None
    category: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    medication_status: MedicationStatus | None = None
    polarity: Literal["PRESENT", "NEGATED", "UNCERTAIN"] | None = None
    certainty: str | None = None
    evidence_ids: list[str] = Field(min_length=1)
    fhir_ref: FhirVersionRef | None = None


class CurrentEncounterContext(StrictModel):
    """Current visit container kept structurally distinct from prior history."""

    encounter_id: str = Field(min_length=1)
    encounter_date: date
    specialty: str = Field(min_length=1)
    assessment_notes: list[str] = Field(default_factory=list)


class ContinuityBriefPayload(StrictModel):
    """Top-level longitudinal continuity brief payload (v1.0.0)."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    patient_id: str = Field(min_length=1)
    continuity_source: ContinuitySource
    staleness_banner: str | None = None
    graph_watermark_ts: datetime | None = None
    prior_history_items: list[PriorHistoryItem] = Field(default_factory=list)
    current_encounter: CurrentEncounterContext
    fhir_version_refs: list[FhirVersionRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_cross_visit_separation(self) -> ContinuityBriefPayload:
        current_id = self.current_encounter.encounter_id
        for item in self.prior_history_items:
            if item.encounter_id == current_id:
                raise ValueError(
                    f"prior_history_items must remain separate from current_encounter ({current_id})"
                )
        return self


__all__ = [
    "ContinuityBriefPayload",
    "ContinuitySource",
    "CurrentEncounterContext",
    "FhirVersionRef",
    "PriorHistoryItem",
]
