"""Zero-PHI allowlist-first structured JSON audit logger."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import json
import logging
from typing import Any
import uuid

from carethread.errors import ErrorCategory, GuidedErrorEnvelope
from carethread.observability.redaction import FailClosedRedactor

FORBIDDEN_LOG_KEYS: frozenset[str] = frozenset(
    {
        "raw_prompt",
        "transcript_text",
        "audio_gcs_path",
        "patient_name",
        "model_output",
    }
)

FREE_TEXT_LOG_FIELDS: frozenset[str] = frozenset({"error_summary"})

ALLOWED_LOG_FIELDS: frozenset[str] = frozenset(
    {
        "action_id",
        "actor_id",
        "attempt",
        "draft_sha256",
        "duration_ms",
        "encounter_id",
        "error_category",
        "error_code",
        "error_summary",
        "field_path",
        "job_state",
        "latency_ms",
        "max_retries_remaining",
        "operation",
        "outcome",
        "payload_digest",
        "purpose",
        "redaction_fail_closed",
        "resource_id",
        "retryable",
        "role",
        "schema_version",
        "sdp_unavailable_fail_closed_count",
        "state",
        "status",
        "target_system",
        "tool_name",
    }
)

_SEVERITY_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _normalize_scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, Enum):
        value = value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


class StructuredAuditLogger:
    """Allowlist-first JSON audit logger with fail-closed residual text scrubbing."""

    def __init__(
        self,
        redactor: FailClosedRedactor,
        sink: list[dict[str, Any]] | None = None,
        logger_name: str = "carethread.audit",
    ) -> None:
        self._redactor = redactor
        self._sink = sink
        self._logger = logging.getLogger(logger_name)

    def log_event(
        self,
        severity: str,
        event_type: str,
        trace_id: str,
        span_id: str,
        tenant_id: str,
        workflow: str,
        job_id: str,
        **fields: Any,
    ) -> dict[str, Any]:
        """Emit an allowlisted structured JSON audit event."""
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": severity,
            "event_type": event_type,
            "trace_id": trace_id,
            "span_id": span_id,
            "tenant_id": tenant_id,
            "workflow": workflow,
            "job_id": job_id,
        }

        for key, raw_value in fields.items():
            if key in FORBIDDEN_LOG_KEYS or key not in ALLOWED_LOG_FIELDS:
                continue

            if key in FREE_TEXT_LOG_FIELDS:
                if raw_value is None:
                    continue
                scrubbed, failed_closed = self._redactor.scrub_free_text(str(raw_value))
                if failed_closed or scrubbed is None:
                    record["redaction_fail_closed"] = True
                    record.setdefault(
                        "error_category",
                        ErrorCategory.SDP_UNAVAILABLE_FAIL_CLOSED.value,
                    )
                else:
                    record[key] = scrubbed
                    record.setdefault("redaction_fail_closed", False)
                continue

            normalized = _normalize_scalar(raw_value)
            if normalized is not None:
                record[key] = normalized

        if self._sink is not None:
            self._sink.append(record)

        level = _SEVERITY_LEVELS.get(severity.upper(), logging.INFO)
        self._logger.log(level, json.dumps(record, sort_keys=True))
        return record

    def log_intent(
        self,
        trace_id: str,
        span_id: str,
        tenant_id: str,
        workflow: str,
        job_id: str,
        action_id: str | None = None,
        severity: str = "INFO",
        **fields: Any,
    ) -> dict[str, Any]:
        """Emit an INTENT audit event and return the record containing action_id."""
        resolved_action_id = action_id or uuid.uuid4().hex
        filtered_fields = dict(fields)
        filtered_fields.pop("event_type", None)
        return self.log_event(
            severity=severity,
            event_type="INTENT",
            trace_id=trace_id,
            span_id=span_id,
            tenant_id=tenant_id,
            workflow=workflow,
            job_id=job_id,
            action_id=resolved_action_id,
            **filtered_fields,
        )

    def log_outcome(
        self,
        trace_id: str,
        span_id: str,
        tenant_id: str,
        workflow: str,
        job_id: str,
        action_id: str,
        outcome: str = "SUCCESS",
        severity: str = "INFO",
        **fields: Any,
    ) -> dict[str, Any]:
        """Emit an OUTCOME audit event paired to an INTENT via action_id."""
        filtered_fields = dict(fields)
        filtered_fields.pop("event_type", None)
        return self.log_event(
            severity=severity,
            event_type="OUTCOME",
            trace_id=trace_id,
            span_id=span_id,
            tenant_id=tenant_id,
            workflow=workflow,
            job_id=job_id,
            action_id=action_id,
            outcome=outcome,
            **filtered_fields,
        )

    def log_guided_error(
        self,
        envelope: GuidedErrorEnvelope,
        span_id: str,
        tenant_id: str,
        workflow: str,
        job_id: str,
        severity: str = "ERROR",
        **fields: Any,
    ) -> dict[str, Any]:
        """Emit a structured error event derived from a GuidedErrorEnvelope."""
        filtered_fields = dict(fields)
        filtered_fields.pop("event_type", None)
        return self.log_event(
            severity=severity,
            event_type="GUIDED_ERROR",
            trace_id=envelope.trace_id,
            span_id=span_id,
            tenant_id=tenant_id,
            workflow=workflow,
            job_id=job_id,
            error_code=envelope.error_code,
            error_category=envelope.category,
            retryable=envelope.retryable,
            max_retries_remaining=envelope.max_retries_remaining,
            field_path=envelope.field_path,
            schema_version=envelope.schema_version,
            **filtered_fields,
        )


__all__ = [
    "ALLOWED_LOG_FIELDS",
    "FORBIDDEN_LOG_KEYS",
    "FREE_TEXT_LOG_FIELDS",
    "StructuredAuditLogger",
]
