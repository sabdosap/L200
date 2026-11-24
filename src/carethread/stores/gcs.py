"""Private GCS artifact store enforcing tenant prefix isolation and CMEK metadata."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Protocol
import uuid

from pydantic import Field

from carethread.errors import ErrorCategory, GuidedErrorEnvelope, GuidedErrorException
from carethread.schemas import StrictModel

DEFAULT_CMEK_KEY_NAME = (
    "projects/carethread/locations/us-central1/keyRings/phi/cryptoKeys/artifacts"
)


class StoredArtifact(StrictModel):
    """Immutable metadata for a private tenant-scoped artifact."""

    uri: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    object_path: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    cmek_key_name: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    version_id: str = Field(min_length=1)
    created_at: datetime
    metadata: dict[str, str] = Field(default_factory=dict)


def _validate_tenant_id(tenant_id: str) -> str:
    """Validate tenant_id contains no path separators, traversal segments, or empty whitespace."""
    if (
        not tenant_id
        or tenant_id != tenant_id.strip()
        or not tenant_id.strip()
        or "/" in tenant_id
        or "\\" in tenant_id
        or ".." in tenant_id
    ):
        raise GuidedErrorException(
            GuidedErrorEnvelope(
                error_code="INVALID_TENANT_ID",
                category=ErrorCategory.AUTHZ_DENIED,
                retryable=False,
                max_retries_remaining=0,
                field_path="tenant_id",
                guided_recovery="Provide a valid non-empty tenant_id without '/' or '..' segments.",
                trace_id=uuid.uuid4().hex,
            )
        )
    return tenant_id


def build_tenant_artifact_path(tenant_id: str, category: str, filename: str) -> str:
    """Build a canonical tenant-scoped object path."""
    clean_tenant = _validate_tenant_id(tenant_id)
    clean_category = category.strip().strip("/")
    clean_filename = filename.strip().lstrip("/")
    if not clean_category or not clean_filename:
        raise GuidedErrorException(
            GuidedErrorEnvelope(
                error_code="INVALID_ARTIFACT_PATH",
                category=ErrorCategory.VALIDATION_RECOVERABLE,
                retryable=False,
                max_retries_remaining=0,
                field_path="object_path",
                guided_recovery="Provide non-empty tenant_id, category, and filename.",
                trace_id=uuid.uuid4().hex,
            )
        )
    return f"tenants/{clean_tenant}/{clean_category}/{clean_filename}"


class PrivateArtifactStore(Protocol):
    """Protocol for private tenant-isolated artifact storage (audio, transcripts, packets)."""

    def put_artifact(
        self,
        tenant_id: str,
        object_path: str,
        data: bytes,
        content_type: str,
        cmek_key_name: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredArtifact:
        """Persist an artifact under the caller's tenant prefix with CMEK metadata."""
        ...

    def get_artifact(
        self,
        tenant_id: str,
        object_path: str,
        version_id: str | None = None,
    ) -> tuple[bytes, StoredArtifact]:
        """Read artifact bytes and metadata for the caller's tenant."""
        ...

    def get_metadata(
        self,
        tenant_id: str,
        object_path: str,
        version_id: str | None = None,
    ) -> StoredArtifact:
        """Read artifact metadata without returning payload bytes."""
        ...

    def list_artifacts(self, tenant_id: str, prefix: str = "") -> list[StoredArtifact]:
        """List latest artifact versions under the caller's tenant namespace."""
        ...


class InMemoryArtifactStore:
    """In-memory PrivateArtifactStore enforcing tenant prefix isolation and CMEK."""

    def __init__(
        self,
        bucket_name: str = "carethread-private-artifacts",
        default_cmek_key_name: str | None = DEFAULT_CMEK_KEY_NAME,
    ) -> None:
        self._bucket_name = bucket_name
        self._default_cmek_key_name = default_cmek_key_name
        self._versions: dict[str, list[tuple[bytes, StoredArtifact]]] = {}

    def _validate_tenant_path(self, tenant_id: str, object_path: str) -> str:
        validated_tenant = _validate_tenant_id(tenant_id)
        normalized = object_path.strip()
        expected_prefix = f"tenants/{validated_tenant}/"
        if (
            not normalized
            or normalized.startswith("/")
            or ".." in normalized.split("/")
            or not normalized.startswith(expected_prefix)
        ):
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="TENANT_PREFIX_VIOLATION",
                    category=ErrorCategory.AUTHZ_DENIED,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="object_path",
                    guided_recovery=(
                        "Artifact paths must remain strictly within "
                        f"tenants/{validated_tenant}/."
                    ),
                    trace_id=uuid.uuid4().hex,
                )
            )
        return normalized

    def _resolve_cmek(self, cmek_key_name: str | None) -> str:
        resolved = self._default_cmek_key_name if cmek_key_name is None else cmek_key_name
        if not resolved or not resolved.strip():
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="CMEK_REQUIRED",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="cmek_key_name",
                    guided_recovery="Configure a non-empty CMEK KMS resource name for private PHI storage.",
                    trace_id=uuid.uuid4().hex,
                )
            )
        return resolved.strip()

    def put_artifact(
        self,
        tenant_id: str,
        object_path: str,
        data: bytes,
        content_type: str,
        cmek_key_name: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredArtifact:
        """Persist an immutable versioned artifact with tenant prefix and CMEK checks."""
        validated_path = self._validate_tenant_path(tenant_id, object_path)
        resolved_cmek = self._resolve_cmek(cmek_key_name)

        history = self._versions.setdefault(validated_path, [])
        version_id = str(len(history) + 1)
        digest = hashlib.sha256(data).hexdigest()
        artifact = StoredArtifact(
            uri=f"gs://{self._bucket_name}/{validated_path}#{version_id}",
            tenant_id=tenant_id,
            object_path=validated_path,
            content_type=content_type,
            cmek_key_name=resolved_cmek,
            sha256=digest,
            size_bytes=len(data),
            version_id=version_id,
            created_at=datetime.now(timezone.utc),
            metadata=dict(metadata or {}),
        )
        history.append((bytes(data), artifact))
        return artifact.model_copy(deep=True)

    def get_artifact(
        self,
        tenant_id: str,
        object_path: str,
        version_id: str | None = None,
    ) -> tuple[bytes, StoredArtifact]:
        """Return artifact bytes and metadata for the validated tenant path."""
        validated_path = self._validate_tenant_path(tenant_id, object_path)
        history = self._versions.get(validated_path)
        if not history:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="ARTIFACT_NOT_FOUND",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="object_path",
                    guided_recovery="Verify the artifact path and version exist before reading.",
                    trace_id=uuid.uuid4().hex,
                )
            )
        if version_id is None:
            payload, meta = history[-1]
            return bytes(payload), meta.model_copy(deep=True)

        for payload, meta in history:
            if meta.version_id == version_id:
                return bytes(payload), meta.model_copy(deep=True)

        raise GuidedErrorException(
            GuidedErrorEnvelope(
                error_code="ARTIFACT_VERSION_NOT_FOUND",
                category=ErrorCategory.VALIDATION_RECOVERABLE,
                retryable=False,
                max_retries_remaining=0,
                field_path="version_id",
                guided_recovery="Request an existing immutable artifact version.",
                trace_id=uuid.uuid4().hex,
            )
        )

    def get_metadata(
        self,
        tenant_id: str,
        object_path: str,
        version_id: str | None = None,
    ) -> StoredArtifact:
        """Return metadata for a stored artifact."""
        _, meta = self.get_artifact(tenant_id, object_path, version_id=version_id)
        return meta

    def list_artifacts(self, tenant_id: str, prefix: str = "") -> list[StoredArtifact]:
        """List latest artifact metadata under the caller's tenant namespace."""
        validated_tenant = _validate_tenant_id(tenant_id)
        base_prefix = f"tenants/{validated_tenant}/"
        full_prefix = base_prefix + prefix.lstrip("/") if prefix else base_prefix
        results: list[StoredArtifact] = []
        for path in sorted(self._versions):
            if path.startswith(full_prefix):
                _, latest_meta = self._versions[path][-1]
                results.append(latest_meta.model_copy(deep=True))
        return results


__all__ = [
    "DEFAULT_CMEK_KEY_NAME",
    "InMemoryArtifactStore",
    "PrivateArtifactStore",
    "StoredArtifact",
    "build_tenant_artifact_path",
]
