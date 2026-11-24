"""Digest-bound, single-use HITL confirmations with TOCTOU invalidation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal
import uuid

from pydantic import Field

from carethread.errors import ErrorCategory, GuidedErrorEnvelope, GuidedErrorException
from carethread.schemas import StrictModel
from carethread.security.consent import ensure_utc
from carethread.session.firestore_session import (
    FirestoreSessionBackend,
    InMemoryFirestoreBackend,
)


class ConfirmationStatus(str, Enum):
    """Lifecycle states of a digest-bound HITL confirmation."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    INVALIDATED_SUPERSEDED = "INVALIDATED_SUPERSEDED"


class HitlConfirmation(StrictModel):
    """Single-use HITL confirmation bound to artifact version, SHA-256 digest, and TTL."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    confirmation_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    artifact_version: int = Field(ge=1)
    sha256_digest: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    status: ConfirmationStatus = ConfirmationStatus.PENDING
    created_at: datetime
    expires_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    rejection_reason: str | None = None
    superseded_by_version: int | None = None
    superseded_by_digest: str | None = None
    superseded_at: datetime | None = None


class HitlConfirmationStore:
    """Durable store enforcing digest-bound, single-use, expiring HITL confirmations."""

    COLLECTION = "hitl_confirmations"

    def __init__(self, backend: FirestoreSessionBackend | None = None) -> None:
        self._backend: FirestoreSessionBackend = (
            backend if backend is not None else InMemoryFirestoreBackend()
        )

    def _now(self, now: datetime | None) -> datetime:
        return ensure_utc(now) if now is not None else datetime.now(timezone.utc)

    def _raise_error(
        self,
        *,
        category: ErrorCategory,
        error_code: str,
        guided_recovery: str,
        field_path: str | None = None,
    ) -> None:
        raise GuidedErrorException(
            GuidedErrorEnvelope(
                error_code=error_code,
                category=category,
                retryable=False,
                max_retries_remaining=0,
                field_path=field_path,
                guided_recovery=guided_recovery,
                trace_id=uuid.uuid4().hex,
            )
        )

    def _save(self, record: HitlConfirmation) -> HitlConfirmation:
        self._backend.put_document(
            self.COLLECTION,
            record.confirmation_id,
            record.model_dump(mode="json"),
        )
        return record.model_copy(deep=True)

    def create_confirmation(
        self,
        artifact_id: str,
        artifact_version: int,
        sha256_digest: str,
        kind: str,
        ttl_seconds: int = 900,
        *,
        confirmation_id: str | None = None,
        now: datetime | None = None,
    ) -> HitlConfirmation:
        """Create a PENDING confirmation bound to (artifact_id, version, sha256_digest, kind, expires_at)."""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if artifact_version < 1:
            raise ValueError("artifact_version must be >= 1")
        if not artifact_id.strip() or not sha256_digest.strip() or not kind.strip():
            raise ValueError("artifact_id, sha256_digest, and kind must be non-empty")

        ts = self._now(now)
        record = HitlConfirmation(
            confirmation_id=confirmation_id or f"hitl-{uuid.uuid4().hex}",
            artifact_id=artifact_id,
            artifact_version=artifact_version,
            sha256_digest=sha256_digest,
            kind=kind,
            status=ConfirmationStatus.PENDING,
            created_at=ts,
            expires_at=ts + timedelta(seconds=ttl_seconds),
        )
        return self._save(record)

    def get_confirmation(self, confirmation_id: str) -> HitlConfirmation:
        """Fetch a persisted confirmation or raise CONFIRMATION_NOT_FOUND."""
        raw = self._backend.get_document(self.COLLECTION, confirmation_id)
        if raw is None:
            self._raise_error(
                category=ErrorCategory.VALIDATION_RECOVERABLE,
                error_code="CONFIRMATION_NOT_FOUND",
                guided_recovery="Verify the confirmation_id exists or request a new HITL confirmation.",
                field_path="confirmation_id",
            )
        return HitlConfirmation.model_validate(raw)

    def approve(
        self,
        confirmation_id: str,
        actor_id: str,
        expected_version: int,
        expected_digest: str,
        now: datetime | None = None,
    ) -> HitlConfirmation:
        """Verify version, digest, expiry, and single-use status before approving."""
        if not actor_id.strip():
            self._raise_error(
                category=ErrorCategory.AUTHZ_DENIED,
                error_code="HITL_ACTOR_REQUIRED",
                guided_recovery="Provide an authenticated actor_id to approve this confirmation.",
                field_path="actor_id",
            )

        ts = self._now(now)
        record = self.get_confirmation(confirmation_id)

        if record.status == ConfirmationStatus.INVALIDATED_SUPERSEDED:
            self._raise_error(
                category=ErrorCategory.HITL_DIGEST_MISMATCH,
                error_code="HITL_CONFIRMATION_SUPERSEDED",
                guided_recovery=(
                    "Artifact was modified since this confirmation was issued; "
                    "review the latest version and issue a fresh confirmation."
                ),
                field_path="sha256_digest",
            )

        if record.artifact_version != expected_version or record.sha256_digest != expected_digest:
            self._raise_error(
                category=ErrorCategory.HITL_DIGEST_MISMATCH,
                error_code="HITL_DIGEST_MISMATCH",
                guided_recovery=(
                    "Approval version or SHA-256 digest does not match the bound confirmation."
                ),
                field_path="sha256_digest",
            )

        if record.status == ConfirmationStatus.APPROVED:
            self._raise_error(
                category=ErrorCategory.AUTHZ_DENIED,
                error_code="HITL_CONFIRMATION_ALREADY_CONSUMED",
                guided_recovery="Confirmation token is single-use and has already been approved.",
                field_path="status",
            )

        if record.status == ConfirmationStatus.REJECTED:
            self._raise_error(
                category=ErrorCategory.AUTHZ_DENIED,
                error_code="HITL_CONFIRMATION_REJECTED",
                guided_recovery="Rejected confirmations cannot be approved; create a new confirmation.",
                field_path="status",
            )

        if record.status == ConfirmationStatus.EXPIRED or ts >= ensure_utc(record.expires_at):
            if record.status == ConfirmationStatus.PENDING:
                self._save(record.model_copy(update={"status": ConfirmationStatus.EXPIRED}))
            self._raise_error(
                category=ErrorCategory.GRANT_EXPIRED,
                error_code="HITL_CONFIRMATION_EXPIRED",
                guided_recovery="Confirmation has expired; request a fresh HITL confirmation.",
                field_path="expires_at",
            )

        updated = record.model_copy(
            update={
                "status": ConfirmationStatus.APPROVED,
                "decided_by": actor_id,
                "decided_at": ts,
            }
        )
        return self._save(updated)

    def reject(
        self,
        confirmation_id: str,
        actor_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> HitlConfirmation:
        """Fail-closed rejection of a pending confirmation with reason and audit actor."""
        if not actor_id.strip():
            self._raise_error(
                category=ErrorCategory.AUTHZ_DENIED,
                error_code="HITL_ACTOR_REQUIRED",
                guided_recovery="Provide an authenticated actor_id to reject this confirmation.",
                field_path="actor_id",
            )
        if not reason.strip():
            self._raise_error(
                category=ErrorCategory.VALIDATION_RECOVERABLE,
                error_code="HITL_REJECTION_REASON_REQUIRED",
                guided_recovery="Provide a non-empty rejection reason.",
                field_path="reason",
            )

        ts = self._now(now)
        record = self.get_confirmation(confirmation_id)

        if record.status == ConfirmationStatus.INVALIDATED_SUPERSEDED:
            self._raise_error(
                category=ErrorCategory.HITL_DIGEST_MISMATCH,
                error_code="HITL_CONFIRMATION_SUPERSEDED",
                guided_recovery="Confirmation was superseded by a newer artifact version.",
                field_path="status",
            )

        if record.status != ConfirmationStatus.PENDING:
            self._raise_error(
                category=ErrorCategory.AUTHZ_DENIED,
                error_code="HITL_CONFIRMATION_NOT_PENDING",
                guided_recovery=f"Cannot reject confirmation in terminal status {record.status.value}.",
                field_path="status",
            )

        if ts >= ensure_utc(record.expires_at):
            self._save(record.model_copy(update={"status": ConfirmationStatus.EXPIRED}))
            self._raise_error(
                category=ErrorCategory.GRANT_EXPIRED,
                error_code="HITL_CONFIRMATION_EXPIRED",
                guided_recovery="Confirmation has expired and cannot be rejected.",
                field_path="expires_at",
            )

        updated = record.model_copy(
            update={
                "status": ConfirmationStatus.REJECTED,
                "decided_by": actor_id,
                "decided_at": ts,
                "rejection_reason": reason,
            }
        )
        return self._save(updated)

    def supersede_for_artifact(
        self,
        artifact_id: str,
        new_version: int,
        new_digest: str,
        now: datetime | None = None,
    ) -> list[HitlConfirmation]:
        """Invalidate all prior open confirmations for an artifact when edited."""
        ts = self._now(now)
        superseded: list[HitlConfirmation] = []
        for raw in self._backend.list_documents(self.COLLECTION):
            record = HitlConfirmation.model_validate(raw)
            if record.artifact_id != artifact_id:
                continue
            if record.status != ConfirmationStatus.PENDING:
                continue
            if record.artifact_version == new_version and record.sha256_digest == new_digest:
                continue

            updated = record.model_copy(
                update={
                    "status": ConfirmationStatus.INVALIDATED_SUPERSEDED,
                    "superseded_by_version": new_version,
                    "superseded_by_digest": new_digest,
                    "superseded_at": ts,
                }
            )
            superseded.append(self._save(updated))
        return superseded


__all__ = [
    "ConfirmationStatus",
    "HitlConfirmation",
    "HitlConfirmationStore",
]
