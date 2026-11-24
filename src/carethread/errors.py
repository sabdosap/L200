"""Versioned guided error envelopes and canonical domain exceptions."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorCategory(str, Enum):
    """Standardized CareThread failure categories for deterministic handling."""

    AUTHZ_DENIED = "AUTHZ_DENIED"
    CONSENT_REVOKED = "CONSENT_REVOKED"
    STALE_GRAPH_DETECTED = "STALE_GRAPH_DETECTED"
    VALIDATION_RECOVERABLE = "VALIDATION_RECOVERABLE"
    HITL_DIGEST_MISMATCH = "HITL_DIGEST_MISMATCH"
    SDP_UNAVAILABLE_FAIL_CLOSED = "SDP_UNAVAILABLE_FAIL_CLOSED"
    GRANT_EXPIRED = "GRANT_EXPIRED"
    OUT_OF_SCOPE_PURPOSE = "OUT_OF_SCOPE_PURPOSE"


class GuidedErrorEnvelope(BaseModel):
    """Structured non-PHI error envelope providing deterministic recovery guidance."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    status: Literal["ERROR"] = "ERROR"
    error_code: str = Field(min_length=1)
    category: ErrorCategory
    retryable: bool
    max_retries_remaining: int = Field(ge=0)
    field_path: str | None = None
    guided_recovery: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)


class GuidedErrorException(Exception):
    """Canonical exception carrying a GuidedErrorEnvelope across CareThread services."""

    def __init__(self, envelope: GuidedErrorEnvelope) -> None:
        self.envelope = envelope
        super().__init__(f"[{envelope.category.value}] {envelope.error_code}: {envelope.guided_recovery}")


__all__ = [
    "ErrorCategory",
    "GuidedErrorEnvelope",
    "GuidedErrorException",
]
