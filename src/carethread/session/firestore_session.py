"""Firestore-backed filtered ADK session state and bounded compaction."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
from typing import Any, Literal, Protocol, runtime_checkable
import uuid

from pydantic import Field

from carethread.errors import ErrorCategory, GuidedErrorEnvelope, GuidedErrorException
from carethread.schemas import StrictModel
from carethread.security.consent import ensure_utc

ALLOWED_SESSION_STATE_KEYS: frozenset[str] = frozenset(
    {
        "tenant_id",
        "encounter_id",
        "job_id",
        "draft_id",
        "draft_version",
        "draft_sha256",
        "artifact_id",
        "artifact_version",
        "artifact_sha256",
        "transcript_ref",
        "transcript_version_id",
        "fhir_prior_refs",
        "active_step",
        "workflow_status",
        "speaker_attribution_status",
        "requested_tool_confirmations",
        "pending_confirmation_ids",
        "confirmation_id",
        "last_trace_id",
        "locale",
        "specialty",
        "metadata",
    }
)

FORBIDDEN_SESSION_STATE_KEYS: frozenset[str] = frozenset(
    {
        "transcript_text",
        "raw_transcript",
        "transcript",
        "audio_gcs_path",
        "raw_audio_path",
        "audio_path",
        "patient_name",
        "patient_dob",
        "date_of_birth",
        "dob",
        "mrn",
        "medical_record_number",
        "ssn",
        "raw_prompt",
        "prompt",
        "raw_model_output",
        "model_output",
        "phi",
        "raw_phi",
        "free_text_phi",
    }
)


def _scan_forbidden_keys(value: Any, path: str = "") -> None:
    """Recursively reject forbidden raw PHI, transcript, audio, prompt, or model output keys."""
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key_str = str(raw_key)
            if key_str.lower() in FORBIDDEN_SESSION_STATE_KEYS:
                loc = f"{path}.{key_str}" if path else key_str
                raise ValueError(
                    f"Forbidden session key '{loc}' cannot store raw PHI, transcript, "
                    "audio path, raw prompt, or raw model output."
                )
            _scan_forbidden_keys(nested, f"{path}.{key_str}" if path else key_str)
    elif isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            _scan_forbidden_keys(item, f"{path}[{idx}]")


def validate_session_state(
    updates: Mapping[str, Any],
    *,
    enforce_allowlist: bool = True,
) -> dict[str, Any]:
    """Validate session state updates via both forbidden-key guard and allowlist."""
    validated: dict[str, Any] = {}
    for raw_key, value in updates.items():
        key = str(raw_key)
        if key.lower() in FORBIDDEN_SESSION_STATE_KEYS:
            raise ValueError(
                f"Forbidden session key '{key}' cannot store raw PHI, transcript, "
                "audio path, raw prompt, or raw model output."
            )
        if enforce_allowlist and key not in ALLOWED_SESSION_STATE_KEYS:
            raise ValueError(
                f"Session state key '{key}' is not in ALLOWED_SESSION_STATE_KEYS."
            )
        _scan_forbidden_keys(value, key)
        validated[key] = json.loads(json.dumps(value))
    return validated


@runtime_checkable
class FirestoreSessionBackend(Protocol):
    """Persistence backend protocol for Firestore-compatible document storage."""

    def get_document(self, collection: str, document_id: str) -> dict[str, Any] | None:
        """Fetch a serialized document dict, or None if absent."""

    def put_document(self, collection: str, document_id: str, data: Mapping[str, Any]) -> None:
        """Persist a document dict durably."""

    def delete_document(self, collection: str, document_id: str) -> None:
        """Delete a document if it exists."""

    def list_documents(self, collection: str) -> list[dict[str, Any]]:
        """List all persisted document dicts in a collection."""


class InMemoryFirestoreBackend:
    """In-memory JSON-serialized Firestore backend surviving store re-instantiation."""

    def __init__(self) -> None:
        self._collections: dict[str, dict[str, str]] = {}

    def get_document(self, collection: str, document_id: str) -> dict[str, Any] | None:
        """Deserialize and return a stored document, or None if not found."""
        raw = self._collections.get(collection, {}).get(document_id)
        if raw is None:
            return None
        return json.loads(raw)

    def put_document(self, collection: str, document_id: str, data: Mapping[str, Any]) -> None:
        """Serialize a document to JSON so caller mutations cannot leak into storage."""
        bucket = self._collections.setdefault(collection, {})
        bucket[document_id] = json.dumps(dict(data), sort_keys=True, default=str)

    def delete_document(self, collection: str, document_id: str) -> None:
        """Remove a document from the in-memory collection if present."""
        bucket = self._collections.get(collection)
        if bucket is not None:
            bucket.pop(document_id, None)

    def list_documents(self, collection: str) -> list[dict[str, Any]]:
        """Return deserialized copies of every document in the collection."""
        bucket = self._collections.get(collection, {})
        return [json.loads(raw) for raw in bucket.values()]


class SessionEvent(StrictModel):
    """Non-PHI ADK turn/event record stored in filtered session history."""

    event_id: str = Field(min_length=1)
    turn_index: int = Field(ge=0)
    role: Literal["user", "model", "tool", "system"]
    content_summary: str = ""
    token_count: int = Field(default=0, ge=0)
    function_call_id: str | None = None
    is_function_call: bool = False
    is_function_response: bool = False
    requested_tool_confirmations: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class SessionRecord(StrictModel):
    """Durable filtered ADK session document (v1.0.0)."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    session_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    state: dict[str, Any] = Field(default_factory=dict)
    events: list[SessionEvent] = Field(default_factory=list)
    next_turn_index: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime


class FilteredSessionStore:
    """Durable filtered ADK session store with strict PHI guards and safe compaction."""

    COLLECTION = "adk_sessions"

    def __init__(self, backend: FirestoreSessionBackend | None = None) -> None:
        self._backend: FirestoreSessionBackend = (
            backend if backend is not None else InMemoryFirestoreBackend()
        )

    def _now(self, now: datetime | None) -> datetime:
        return ensure_utc(now) if now is not None else datetime.now(timezone.utc)

    def _save(self, record: SessionRecord) -> SessionRecord:
        self._backend.put_document(
            self.COLLECTION,
            record.session_id,
            record.model_dump(mode="json"),
        )
        return record.model_copy(deep=True)

    def create_session(
        self,
        session_id: str,
        tenant_id: str,
        user_id: str,
        state: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> SessionRecord:
        """Create and persist a new filtered ADK session."""
        if self._backend.get_document(self.COLLECTION, session_id) is not None:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="SESSION_ALREADY_EXISTS",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="session_id",
                    guided_recovery="Load the existing session or choose a unique session_id.",
                    trace_id=uuid.uuid4().hex,
                )
            )
        ts = self._now(now)
        validated_state = validate_session_state(state or {})
        record = SessionRecord(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            state=validated_state,
            events=[],
            next_turn_index=0,
            created_at=ts,
            updated_at=ts,
        )
        return self._save(record)

    def get_session(self, session_id: str) -> SessionRecord:
        """Load and validate a persisted session from the backend."""
        raw = self._backend.get_document(self.COLLECTION, session_id)
        if raw is None:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="SESSION_NOT_FOUND",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="session_id",
                    guided_recovery="Verify the session_id exists in FilteredSessionStore.",
                    trace_id=uuid.uuid4().hex,
                )
            )
        return SessionRecord.model_validate(raw)

    def update_state(
        self,
        session_id: str,
        updates: Mapping[str, Any],
        now: datetime | None = None,
    ) -> SessionRecord:
        """Merge allowlisted non-PHI metadata updates into session state."""
        record = self.get_session(session_id)
        validated_updates = validate_session_state(updates)
        merged_state = dict(record.state)
        merged_state.update(validated_updates)
        updated = record.model_copy(
            update={
                "state": merged_state,
                "updated_at": self._now(now),
            }
        )
        return self._save(updated)

    def append_event(
        self,
        session_id: str,
        role: Literal["user", "model", "tool", "system"],
        content_summary: str = "",
        token_count: int | None = None,
        *,
        event_id: str | None = None,
        function_call_id: str | None = None,
        is_function_call: bool = False,
        is_function_response: bool = False,
        requested_tool_confirmations: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> SessionEvent:
        """Append a non-PHI turn/event and update active requested_tool_confirmations."""
        record = self.get_session(session_id)
        confirmations = dict(requested_tool_confirmations or {})
        meta = dict(metadata or {})
        _scan_forbidden_keys(confirmations, "requested_tool_confirmations")
        _scan_forbidden_keys(meta, "metadata")

        resolved_tokens = (
            token_count
            if token_count is not None
            else (max(1, (len(content_summary) + 3) // 4) if content_summary else 0)
        )
        ts = self._now(now)
        event = SessionEvent(
            event_id=event_id or f"evt-{uuid.uuid4().hex}",
            turn_index=record.next_turn_index,
            role=role,
            content_summary=content_summary,
            token_count=resolved_tokens,
            function_call_id=function_call_id,
            is_function_call=is_function_call,
            is_function_response=is_function_response,
            requested_tool_confirmations=json.loads(json.dumps(confirmations)),
            metadata=json.loads(json.dumps(meta)),
            created_at=ts,
        )

        merged_state = dict(record.state)
        if confirmations:
            existing_confirmations = dict(merged_state.get("requested_tool_confirmations") or {})
            existing_confirmations.update(event.requested_tool_confirmations)
            merged_state["requested_tool_confirmations"] = existing_confirmations

        updated = record.model_copy(
            update={
                "state": merged_state,
                "events": [*record.events, event],
                "next_turn_index": record.next_turn_index + 1,
                "updated_at": ts,
            }
        )
        self._save(updated)
        return event.model_copy(deep=True)

    def resolve_tool_confirmation(
        self,
        session_id: str,
        call_id: str,
        now: datetime | None = None,
    ) -> SessionRecord:
        """Mark an active tool confirmation call_id as resolved in session state."""
        record = self.get_session(session_id)
        merged_state = dict(record.state)
        active = dict(merged_state.get("requested_tool_confirmations") or {})
        active.pop(call_id, None)
        merged_state["requested_tool_confirmations"] = active
        updated = record.model_copy(
            update={
                "state": merged_state,
                "updated_at": self._now(now),
            }
        )
        return self._save(updated)

    def _is_protected_event(
        self,
        event: SessionEvent,
        active_confirmation_ids: set[str],
        resolved_function_call_ids: set[str],
    ) -> bool:
        if event.function_call_id and event.function_call_id in active_confirmation_ids:
            return True
        if event.requested_tool_confirmations and any(
            call_id in active_confirmation_ids or call_id not in resolved_function_call_ids
            for call_id in event.requested_tool_confirmations
        ):
            return True
        if (
            event.is_function_call
            and event.function_call_id is not None
            and event.function_call_id not in resolved_function_call_ids
        ):
            return True
        return bool(event.metadata.get("unresolved_function_response"))

    def compact_events(
        self,
        session_id: str,
        max_turns: int = 6,
        max_tokens: int = 16000,
        now: datetime | None = None,
    ) -> SessionRecord:
        """Trim older turns to fit limits while preserving active HITL and unresolved calls."""
        if max_turns < 1 or max_tokens < 1:
            raise ValueError("max_turns and max_tokens must both be >= 1")

        record = self.get_session(session_id)
        events = record.events
        if not events:
            return record

        active_confirmations = set((record.state.get("requested_tool_confirmations") or {}).keys())
        resolved_calls = {
            event.function_call_id
            for event in events
            if event.is_function_response and event.function_call_id is not None
        }

        protected_indices: set[int] = set()
        trimmable_indices: list[int] = []
        for idx, event in enumerate(events):
            if self._is_protected_event(event, active_confirmations, resolved_calls):
                protected_indices.add(idx)
            else:
                trimmable_indices.append(idx)

        turn_budget = max(0, max_turns - len(protected_indices))
        protected_tokens = sum(events[idx].token_count for idx in protected_indices)
        token_budget = max(0, max_tokens - protected_tokens)

        kept_trimmable: set[int] = set()
        used_tokens = 0
        for idx in reversed(trimmable_indices):
            if len(kept_trimmable) >= turn_budget:
                break
            tokens = events[idx].token_count
            if used_tokens + tokens > token_budget:
                break
            kept_trimmable.add(idx)
            used_tokens += tokens

        kept_indices = sorted(protected_indices | kept_trimmable)
        compacted_events = [events[idx] for idx in kept_indices]

        updated = record.model_copy(
            update={
                "events": compacted_events,
                "updated_at": self._now(now),
            }
        )
        return self._save(updated)


__all__ = [
    "ALLOWED_SESSION_STATE_KEYS",
    "FORBIDDEN_SESSION_STATE_KEYS",
    "FilteredSessionStore",
    "FirestoreSessionBackend",
    "InMemoryFirestoreBackend",
    "SessionEvent",
    "SessionRecord",
    "validate_session_state",
]
