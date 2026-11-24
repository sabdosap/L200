"""Deterministic tenant isolation, care-relationship verification, and HMAC-signed capability tokens."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
import hashlib
import hmac
import json
from typing import Literal
import uuid

from pydantic import ConfigDict, Field

from carethread.errors import ErrorCategory
from carethread.schemas import StrictModel
from carethread.security.consent import (
    ConsentRecord,
    ConsentStatus,
    ensure_utc,
    raise_security_error,
)
from carethread.security.secrets import SecretManagerClient

ALLOWED_CLINICAL_ROLES: frozenset[str] = frozenset(
    {
        "CLINICIAN",
        "PHYSICIAN",
        "NURSE_PRACTITIONER",
        "PHYSICIAN_ASSISTANT",
        "RECORDS_REVIEWER",
    }
)


class ActorContext(StrictModel):
    """Authenticated caller identity, tenant boundary, role, and active care panel."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    specialty: str | None = None
    care_patient_ids: frozenset[str] = Field(default_factory=frozenset)


class ScopeCapabilityToken(StrictModel):
    """Short-lived HMAC-SHA256-signed authorization capability scoped to one patient and consent version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    token_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    allowed_categories: frozenset[str] = Field(default_factory=frozenset)
    consent_version_id: str = Field(min_length=1)
    expires_at: datetime
    signature: str = Field(min_length=1)


def _canonical_token_payload(
    *,
    schema_version: str,
    token_id: str,
    tenant_id: str,
    patient_id: str,
    actor_id: str,
    allowed_categories: frozenset[str],
    consent_version_id: str,
    expires_at: datetime,
) -> bytes:
    payload = {
        "schema_version": schema_version,
        "token_id": token_id,
        "tenant_id": tenant_id,
        "patient_id": patient_id,
        "actor_id": actor_id,
        "allowed_categories": sorted(allowed_categories),
        "consent_version_id": consent_version_id,
        "expires_at": ensure_utc(expires_at).isoformat(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


class AuthzService:
    """Deterministic policy decision point issuing and verifying ScopeCapabilityTokens."""

    def __init__(
        self,
        secret_manager: SecretManagerClient,
        secret_project_id: str,
        secret_id: str,
        secret_version: str = "latest",
        token_ttl: timedelta = timedelta(minutes=15),
        allowed_roles: frozenset[str] = ALLOWED_CLINICAL_ROLES,
    ) -> None:
        if not secret_project_id or not secret_id or not secret_version:
            raise ValueError("secret_project_id, secret_id, and secret_version must be non-empty")
        if token_ttl <= timedelta(0):
            raise ValueError("token_ttl must be positive")
        self._secret_manager = secret_manager
        self._secret_project_id = secret_project_id
        self._secret_id = secret_id
        self._secret_version = secret_version
        self._token_ttl = token_ttl
        self._allowed_roles = frozenset(role.upper() for role in allowed_roles)

    def authorize_clinical_access(
        self,
        actor: ActorContext,
        patient_id: str,
        patient_tenant_id: str,
        consent_record: ConsentRecord,
        now: datetime,
        requested_categories: Iterable[str] | None = None,
        trace_id: str | None = None,
    ) -> ScopeCapabilityToken:
        """Verify tenant isolation, clinical role, care relationship, and active consent before issuing a token."""
        if actor.tenant_id != patient_tenant_id:
            raise_security_error(
                category=ErrorCategory.AUTHZ_DENIED,
                error_code="TENANT_ISOLATION_VIOLATION",
                field_path="actor.tenant_id",
                guided_recovery="Cross-tenant clinical access is prohibited; authenticate within the patient's tenant.",
                trace_id=trace_id,
            )

        if consent_record.tenant_id != patient_tenant_id or consent_record.patient_id != patient_id:
            raise_security_error(
                category=ErrorCategory.AUTHZ_DENIED,
                error_code="CONSENT_SUBJECT_MISMATCH",
                field_path="consent_record",
                guided_recovery="Provide the active consent record matching the target patient and tenant.",
                trace_id=trace_id,
            )

        if actor.role.strip().upper() not in self._allowed_roles:
            raise_security_error(
                category=ErrorCategory.AUTHZ_DENIED,
                error_code="ROLE_NOT_AUTHORIZED",
                field_path="actor.role",
                guided_recovery="Clinical continuity access requires an authorized clinical role.",
                trace_id=trace_id,
            )

        if patient_id not in actor.care_patient_ids:
            raise_security_error(
                category=ErrorCategory.AUTHZ_DENIED,
                error_code="CARE_RELATIONSHIP_REQUIRED",
                field_path="actor.care_patient_ids",
                guided_recovery="Access denied because the actor has no documented care relationship with the patient.",
                trace_id=trace_id,
            )

        if consent_record.status != ConsentStatus.ACTIVE:
            raise_security_error(
                category=ErrorCategory.CONSENT_REVOKED,
                error_code="PATIENT_CONSENT_REVOKED",
                field_path="consent_record.status",
                guided_recovery="Patient consent is revoked; halt retrieval and do not expose clinical records.",
                trace_id=trace_id,
            )

        if "clinical_care" not in consent_record.allowed_purposes:
            raise_security_error(
                category=ErrorCategory.CONSENT_REVOKED,
                error_code="CONSENT_CLINICAL_CARE_NOT_AUTHORIZED",
                field_path="consent_record.allowed_purposes",
                guided_recovery="Obtain active patient consent explicitly permitting 'clinical_care'.",
                trace_id=trace_id,
            )

        requested = frozenset(requested_categories) if requested_categories is not None else None
        if requested is not None and not requested.issubset(consent_record.allowed_categories):
            raise_security_error(
                category=ErrorCategory.AUTHZ_DENIED,
                error_code="REQUESTED_CATEGORIES_EXCEED_CONSENT",
                field_path="requested_categories",
                guided_recovery="Limit requested categories to those covered by active patient consent.",
                trace_id=trace_id,
            )
        allowed_categories = (
            requested if requested is not None else consent_record.allowed_categories
        )

        signing_key = self._fetch_signing_key()
        now_utc = ensure_utc(now)
        expires_at = now_utc + self._token_ttl
        token_id = f"TOK-{uuid.uuid4().hex[:16]}"
        schema_version: Literal["1.0.0"] = "1.0.0"

        canonical_bytes = _canonical_token_payload(
            schema_version=schema_version,
            token_id=token_id,
            tenant_id=patient_tenant_id,
            patient_id=patient_id,
            actor_id=actor.actor_id,
            allowed_categories=allowed_categories,
            consent_version_id=consent_record.consent_version_id,
            expires_at=expires_at,
        )
        signature = hmac.new(signing_key, canonical_bytes, hashlib.sha256).hexdigest()

        return ScopeCapabilityToken(
            schema_version=schema_version,
            token_id=token_id,
            tenant_id=patient_tenant_id,
            patient_id=patient_id,
            actor_id=actor.actor_id,
            allowed_categories=allowed_categories,
            consent_version_id=consent_record.consent_version_id,
            expires_at=expires_at,
            signature=signature,
        )

    def verify_token(self, token: ScopeCapabilityToken, now: datetime) -> bool:
        """Verify HMAC-SHA256 token integrity and expiry deterministically."""
        now_utc = ensure_utc(now)
        if now_utc >= ensure_utc(token.expires_at):
            return False

        signing_key = self._fetch_signing_key()
        canonical_bytes = _canonical_token_payload(
            schema_version=token.schema_version,
            token_id=token.token_id,
            tenant_id=token.tenant_id,
            patient_id=token.patient_id,
            actor_id=token.actor_id,
            allowed_categories=token.allowed_categories,
            consent_version_id=token.consent_version_id,
            expires_at=token.expires_at,
        )
        expected_signature = hmac.new(signing_key, canonical_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(token.signature, expected_signature)

    def _fetch_signing_key(self) -> bytes:
        raw_secret = self._secret_manager.get_secret(
            self._secret_project_id,
            self._secret_id,
            self._secret_version,
        )
        return raw_secret.encode("utf-8") if isinstance(raw_secret, str) else bytes(raw_secret)


__all__ = [
    "ALLOWED_CLINICAL_ROLES",
    "ActorContext",
    "AuthzService",
    "ScopeCapabilityToken",
]
