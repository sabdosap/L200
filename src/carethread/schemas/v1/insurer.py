"""Synthetic insurer release packet and documentation coverage checklist contracts (v1.0.0)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from carethread.schemas import StrictModel


class CoverageChecklist(StrictModel):
    """Deterministic documentation completeness summary for prior authorization."""

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    missing_items: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_bounds(self) -> CoverageChecklist:
        if self.numerator > self.denominator:
            raise ValueError("numerator must be less than or equal to denominator")
        return self


class AllowlistedPacketItem(StrictModel):
    """Signed FHIR R4 resource summary permitted for insurer release."""

    resource_type: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    encounter_date: date | None = None
    summary: str = Field(min_length=1)


class InsurerPacketPayload(StrictModel):
    """Top-level allowlisted insurer release packet payload (v1.0.0)."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    packet_id: str = Field(min_length=1)
    packet_version: int = Field(ge=1)
    grant_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    consent_version_id: str = Field(min_length=1)
    purpose: Literal["prior_auth_documentation"] = "prior_auth_documentation"
    status: str = Field(default="AWAITING_HUMAN_RELEASE", min_length=1)
    allowlisted_items: list[AllowlistedPacketItem] = Field(default_factory=list)
    coverage_checklist: CoverageChecklist
    packet_sha256: str | None = None


__all__ = [
    "AllowlistedPacketItem",
    "CoverageChecklist",
    "InsurerPacketPayload",
]
