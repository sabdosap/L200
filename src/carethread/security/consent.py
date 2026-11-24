"""Patient consent records and expiring purpose-bound insurer release grants."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal, NoReturn
import uuid

from pydantic import ConfigDict, Field

from carethread.errors import ErrorCategory, GuidedErrorEnvelope, GuidedErrorException
from carethread.schemas import StrictModel

ALLOWED_INSURER_PURPOSES: frozenset[str] = frozenset({"prior_auth_documentation"})
DEFAULT_GRANT_TTL_HOURS: int = 72


def ensure_utc(value: datetime) -> datetime:
    """Normalize a datetime to UTC so comparisons remain deterministic."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def raise_security_error(
    *,
    category: ErrorCategory,
    error_code: str,
    guided_recovery: str,
    field_path: str | None = None,
    trace_id: str | None = None,
) -> NoReturn:
    """Raise a non-retryable GuidedErrorException with a canonical non-PHI envelope."""
    raise GuidedErrorException(
        GuidedErrorEnvelope(
            error_code=error_code,
            category=category,
            retryable=False,
            max_retries_remaining=0,
            field_path=field_path,
            guided_recovery=guided_recovery,
            trace_id=trace_id or uuid.uuid4().hex,
        )
    )


class ConsentStatus(str, Enum):
    """Lifecycle status of a patient's authorization record."""

    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class GrantStatus(str, Enum):
    """Lifecycle status of a purpose-bound insurer release grant."""

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class ConsentRecord(StrictModel):
    """Versioned patient consent record governing clinical and insurer release access."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    consent_version_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    status: ConsentStatus = ConsentStatus.ACTIVE
    allowed_categories: frozenset[str] = Field(default_factory=frozenset)
    allowed_purposes: frozenset[str] = Field(
        default_factory=lambda: frozenset({"clinical_care", "prior_auth_documentation"})
    )
    updated_at: datetime | None = None


class ReleaseGrant(StrictModel):
    """Expiring, purpose-bound, consent-version-pinned insurer release grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    grant_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    insurer_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    allowed_categories: frozenset[str] = Field(default_factory=frozenset)
    consent_version_id: str = Field(min_length=1)
    created_at: datetime
    expires_at: datetime
    status: GrantStatus = GrantStatus.ACTIVE


class GrantService:
    """Deterministic validator for purpose-bound expiring insurer release grants."""

    def __init__(self, default_ttl: timedelta = timedelta(hours=DEFAULT_GRANT_TTL_HOURS)) -> None:
        if default_ttl <= timedelta(0):
            raise ValueError("default_ttl must be positive")
        self._default_ttl = default_ttl

    def create_grant(
        self,
        tenant_id: str,
        patient_id: str,
        insurer_id: str,
        purpose: str,
        consent_record: ConsentRecord,
        now: datetime,
        allowed_categories: Iterable[str] | None = None,
        ttl: timedelta | None = None,
        grant_id: str | None = None,
        trace_id: str | None = None,
    ) -> ReleaseGrant:
        """Create an expiring grant only if purpose is allowlisted and patient consent is active."""
        self._validate_purpose(purpose, trace_id=trace_id)
        self._validate_consent_binding(
            tenant_id=tenant_id,
            patient_id=patient_id,
            purpose=purpose,
            consent_record=consent_record,
            trace_id=trace_id,
        )

        effective_ttl = ttl if ttl is not None else self._default_ttl
        if effective_ttl <= timedelta(0):
            raise ValueError("Grant ttl must be positive")

        requested = frozenset(allowed_categories) if allowed_categories is not None else None
        if requested is not None and consent_record.allowed_categories:
            if not requested.issubset(consent_record.allowed_categories):
                raise_security_error(
                    category=ErrorCategory.OUT_OF_SCOPE_PURPOSE,
                    error_code="INSURER_CATEGORY_OUT_OF_SCOPE",
                    field_path="allowed_categories",
                    guided_recovery="Request only categories permitted by the active patient consent record.",
                    trace_id=trace_id,
                )
        resolved_categories = (
            requested if requested is not None else consent_record.allowed_categories
        )

        created_utc = ensure_utc(now)
        return ReleaseGrant(
            grant_id=grant_id or f"GR-{uuid.uuid4().hex[:12].upper()}",
            tenant_id=tenant_id,
            patient_id=patient_id,
            insurer_id=insurer_id,
            purpose=purpose,
            allowed_categories=resolved_categories,
            consent_version_id=consent_record.consent_version_id,
            created_at=created_utc,
            expires_at=created_utc + effective_ttl,
            status=GrantStatus.ACTIVE,
        )

    def verify_grant_active(
        self,
        grant: ReleaseGrant,
        consent_record: ConsentRecord,
        now: datetime,
        trace_id: str | None = None,
    ) -> ReleaseGrant:
        """Verify grant purpose, exact consent version, active consent, and non-expiry."""
        self._validate_purpose(grant.purpose, trace_id=trace_id)

        if grant.status == GrantStatus.REVOKED:
            raise_security_error(
                category=ErrorCategory.CONSENT_REVOKED,
                error_code="GRANT_REVOKED",
                field_path="grant.status",
                guided_recovery="Request a new release grant backed by active patient consent.",
                trace_id=trace_id,
            )
        if grant.status == GrantStatus.EXPIRED:
            raise_security_error(
                category=ErrorCategory.GRANT_EXPIRED,
                error_code="GRANT_EXPIRED",
                field_path="grant.status",
                guided_recovery="Create a new time-bounded release grant with active patient consent.",
                trace_id=trace_id,
            )

        self._validate_consent_binding(
            tenant_id=grant.tenant_id,
            patient_id=grant.patient_id,
            purpose=grant.purpose,
            consent_record=consent_record,
            trace_id=trace_id,
        )

        # Pin grants to the exact consent version authorized at creation so any
        # subsequent consent edit or revocation fails closed before packet release.
        if consent_record.consent_version_id != grant.consent_version_id:
            raise_security_error(
                category=ErrorCategory.CONSENT_REVOKED,
                error_code="CONSENT_VERSION_SUPERSEDED",
                field_path="consent_record.consent_version_id",
                guided_recovery=(
                    "Patient consent version changed; obtain a fresh release grant "
                    "bound to the current consent_version_id."
                ),
                trace_id=trace_id,
            )

        now_utc = ensure_utc(now)
        if now_utc >= ensure_utc(grant.expires_at):
            raise_security_error(
                category=ErrorCategory.GRANT_EXPIRED,
                error_code="GRANT_EXPIRED",
                field_path="grant.expires_at",
                guided_recovery="Create a new time-bounded release grant with active patient consent.",
                trace_id=trace_id,
            )

        return grant

    @staticmethod
    def _validate_purpose(purpose: str, *, trace_id: str | None) -> None:
        if purpose not in ALLOWED_INSURER_PURPOSES:
            raise_security_error(
                category=ErrorCategory.OUT_OF_SCOPE_PURPOSE,
                error_code="INSURER_PURPOSE_OUT_OF_SCOPE",
                field_path="purpose",
                guided_recovery=(
                    "Insurer grants are restricted to allowlisted purposes: "
                    f"{', '.join(sorted(ALLOWED_INSURER_PURPOSES))}."
                ),
                trace_id=trace_id,
            )

    @staticmethod
    def _validate_consent_binding(
        *,
        tenant_id: str,
        patient_id: str,
        purpose: str,
        consent_record: ConsentRecord,
        trace_id: str | None,
    ) -> None:
        if consent_record.tenant_id != tenant_id or consent_record.patient_id != patient_id:
            raise_security_error(
                category=ErrorCategory.AUTHZ_DENIED,
                error_code="CONSENT_SUBJECT_MISMATCH",
                field_path="consent_record",
                guided_recovery="Supply a consent record matching the target tenant_id and patient_id.",
                trace_id=trace_id,
            )
        if consent_record.status != ConsentStatus.ACTIVE:
            raise_security_error(
                category=ErrorCategory.CONSENT_REVOKED,
                error_code="PATIENT_CONSENT_REVOKED",
                field_path="consent_record.status",
                guided_recovery="Patient consent is revoked; halt release and do not serve clinical records.",
                trace_id=trace_id,
            )
        if purpose not in consent_record.allowed_purposes:
            raise_security_error(
                category=ErrorCategory.OUT_OF_SCOPE_PURPOSE,
                error_code="CONSENT_PURPOSE_NOT_AUTHORIZED",
                field_path="consent_record.allowed_purposes",
                guided_recovery="Obtain patient consent explicitly permitting the requested release purpose.",
                trace_id=trace_id,
            )


__all__ = [
    "ALLOWED_INSURER_PURPOSES",
    "ConsentRecord",
    "ConsentStatus",
    "DEFAULT_GRANT_TTL_HOURS",
    "GrantService",
    "GrantStatus",
    "ReleaseGrant",
    "ensure_utc",
    "raise_security_error",
]
