"""Secret Manager client protocol and deterministic in-memory implementation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretManagerClient(Protocol):
    """Protocol for resolving versioned secrets without hardcoded fallbacks."""

    def get_secret(self, project_id: str, secret_id: str, version: str = "latest") -> bytes:
        """Return the raw secret bytes for `(project_id, secret_id, version)` or raise `KeyError`."""


class InMemorySecretManagerClient:
    """Deterministic in-memory Secret Manager client for offline tests and local runs."""

    def __init__(self, secrets: Mapping[tuple[str, str, str], bytes | str] | None = None) -> None:
        self._secrets: dict[tuple[str, str, str], bytes] = {}
        if secrets:
            for (project_id, secret_id, version), value in secrets.items():
                self.set_secret(project_id, secret_id, version, value)

    def set_secret(self, project_id: str, secret_id: str, version: str, value: bytes | str) -> None:
        """Register a non-empty versioned secret payload."""
        if not project_id or not secret_id or not version:
            raise ValueError("project_id, secret_id, and version must be non-empty strings")
        secret_bytes = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        if not secret_bytes:
            raise ValueError("secret payload must not be empty")
        self._secrets[(project_id, secret_id, version)] = secret_bytes

    def get_secret(self, project_id: str, secret_id: str, version: str = "latest") -> bytes:
        """Resolve `(project_id, secret_id, version)` strictly; raises `KeyError` when absent."""
        key = (project_id, secret_id, version)
        if key not in self._secrets:
            raise KeyError(
                f"Secret not found: projects/{project_id}/secrets/{secret_id}/versions/{version}"
            )
        return self._secrets[key]


__all__ = [
    "InMemorySecretManagerClient",
    "SecretManagerClient",
]
