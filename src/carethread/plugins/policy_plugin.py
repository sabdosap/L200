"""ADK BasePlugin-compatible policy gate with caller allowlists, token checks, and INTENT/OUTCOME logging."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any
import uuid

from pydantic import ConfigDict, Field

from carethread.errors import ErrorCategory, GuidedErrorEnvelope, GuidedErrorException
from carethread.observability.logger import StructuredAuditLogger
from carethread.prompts.constitution_v1 import (
    has_untrusted_delimiter_markers,
    verify_untrusted_delimiters,
)
from carethread.routing.model_router import FLASH_MAX_INPUT_TOKENS
from carethread.schemas import StrictModel
from carethread.security.authz import ScopeCapabilityToken
from carethread.security.consent import ensure_utc

try:
    from google.adk.plugins import BasePlugin as _AdkBasePlugin
except ImportError:  # pragma: no cover - exercised in offline environments without google-adk

    class _AdkBasePlugin:  # type: ignore[no-redef]
        """Lightweight ADK BasePlugin-compatible base class for offline execution."""

        def __init__(self, name: str = "carethread_policy_plugin") -> None:
            self.name = name


DEFAULT_CALLER_ALLOWLIST: dict[str, frozenset[str]] = {
    "fetch_verbatim_transcript_segments": frozenset(
        {"soap_extractor_agent", "evidence_verifier_agent"}
    ),
    "validate_soap_draft_grounding": frozenset(
        {"evidence_verifier_agent", "adk_coordinator"}
    ),
    "request_clinician_signoff_confirmation": frozenset({"adk_coordinator"}),
    "query_patient_continuity_graph": frozenset({"continuity_retrieval_agent"}),
    "verify_fhir_resource_versions": frozenset(
        {"continuity_retrieval_agent", "evidence_verifier_agent"}
    ),
    "fetch_authoritative_fhir_history": frozenset({"continuity_retrieval_agent"}),
    "assemble_allowlisted_insurer_packet": frozenset({"records_release_coordinator"}),
    "request_release_hitl_confirmation": frozenset({"records_release_coordinator"}),
}


DEFAULT_TOOLS_REQUIRING_SCOPE_TOKEN: frozenset[str] = frozenset(
    {
        "query_patient_continuity_graph",
        "verify_fhir_resource_versions",
        "fetch_authoritative_fhir_history",
    }
)


class PolicyDecision(StrictModel):
    """Result returned when a tool or model policy check passes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str = Field(min_length=1)
    allowed: bool = True
    caller_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    tool_name: str | None = None
    model_id: str | None = None
    estimated_tokens: int | None = None


def _estimate_token_count(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _verify_token_integrity(
    token_verifier: Any,
    token: ScopeCapabilityToken,
    now_utc: datetime,
) -> bool:
    if token_verifier is None:
        return False
    if hasattr(token_verifier, "verify_token"):
        return bool(token_verifier.verify_token(token, now_utc))
    if callable(token_verifier):
        return bool(token_verifier(token, now_utc))
    return False


class CareThreadPolicyPlugin(_AdkBasePlugin):
    """Deterministic ADK policy plugin enforcing allowlists, capability tokens, prompt budgets, and audit logs."""

    def __init__(
        self,
        audit_logger: StructuredAuditLogger,
        caller_allowlist: Mapping[str, Iterable[str]] | None = None,
        token_verifier: Any = None,
        *,
        max_prompt_tokens: int = FLASH_MAX_INPUT_TOKENS,
        tools_requiring_scope_token: Iterable[str] | None = None,
        name: str = "carethread_policy_plugin",
    ) -> None:
        super().__init__(name=name)
        if max_prompt_tokens <= 0:
            raise ValueError("max_prompt_tokens must be positive")
        self._audit_logger = audit_logger
        source_allowlist = (
            DEFAULT_CALLER_ALLOWLIST if caller_allowlist is None else caller_allowlist
        )
        self._caller_allowlist: dict[str, frozenset[str]] = {
            tool: frozenset(callers) for tool, callers in source_allowlist.items()
        }
        self._token_verifier = token_verifier
        self._max_prompt_tokens = max_prompt_tokens
        self._tools_requiring_scope_token = (
            DEFAULT_TOOLS_REQUIRING_SCOPE_TOKEN
            if tools_requiring_scope_token is None
            else frozenset(tools_requiring_scope_token)
        )

    def _deny_and_raise(
        self,
        *,
        category: ErrorCategory,
        error_code: str,
        guided_recovery: str,
        field_path: str,
        trace_id: str,
        span_id: str,
        tenant_id: str,
        workflow: str,
        job_id: str,
        action_id: str,
        caller_id: str,
        tool_name: str | None = None,
        target_system: str | None = None,
        operation: str,
    ) -> None:
        self._audit_logger.log_outcome(
            trace_id=trace_id,
            span_id=span_id,
            tenant_id=tenant_id,
            workflow=workflow,
            job_id=job_id,
            action_id=action_id,
            outcome="DENIED",
            severity="WARNING",
            actor_id=caller_id,
            tool_name=tool_name,
            target_system=target_system,
            operation=operation,
            error_category=category,
            error_code=error_code,
            error_summary=guided_recovery,
        )
        raise GuidedErrorException(
            GuidedErrorEnvelope(
                error_code=error_code,
                category=category,
                retryable=False,
                max_retries_remaining=0,
                field_path=field_path,
                guided_recovery=guided_recovery,
                trace_id=trace_id,
            )
        )

    def before_tool_call(
        self,
        *,
        caller_id: str,
        tool_name: str,
        tenant_id: str = "tenant-unknown",
        workflow: str = "adk_workflow",
        job_id: str = "job-unknown",
        trace_id: str | None = None,
        span_id: str | None = None,
        scope_token: ScopeCapabilityToken | None = None,
        require_scope_token: bool | None = None,
        patient_id: str | None = None,
        actor_id: str | None = None,
        encounter_id: str | None = None,
        now: datetime | None = None,
    ) -> PolicyDecision:
        """Verify caller allowlist and scope token before tool execution, logging ACTION_INTENT."""
        resolved_trace = trace_id or uuid.uuid4().hex
        resolved_span = span_id or uuid.uuid4().hex[:16]
        intent_record = self._audit_logger.log_intent(
            trace_id=resolved_trace,
            span_id=resolved_span,
            tenant_id=tenant_id,
            workflow=workflow,
            job_id=job_id,
            actor_id=caller_id,
            tool_name=tool_name,
            encounter_id=encounter_id,
            operation="tool_call",
        )
        action_id = intent_record["action_id"]

        allowed_callers = self._caller_allowlist.get(tool_name)
        if not allowed_callers or caller_id not in allowed_callers:
            self._deny_and_raise(
                category=ErrorCategory.AUTHZ_DENIED,
                error_code="CALLER_NOT_ALLOWLISTED",
                guided_recovery=(
                    f"Caller '{caller_id}' is not permitted to invoke tool '{tool_name}'."
                ),
                field_path="caller_id",
                trace_id=resolved_trace,
                span_id=resolved_span,
                tenant_id=tenant_id,
                workflow=workflow,
                job_id=job_id,
                action_id=action_id,
                caller_id=caller_id,
                tool_name=tool_name,
                operation="tool_call",
            )

        must_verify_token = (
            require_scope_token
            if require_scope_token is not None
            else tool_name in self._tools_requiring_scope_token
        )
        if must_verify_token and scope_token is None:
            self._deny_and_raise(
                category=ErrorCategory.AUTHZ_DENIED,
                error_code="MISSING_SCOPE_CAPABILITY_TOKEN",
                guided_recovery=(
                    f"Tool '{tool_name}' requires a valid ScopeCapabilityToken."
                ),
                field_path="scope_token",
                trace_id=resolved_trace,
                span_id=resolved_span,
                tenant_id=tenant_id,
                workflow=workflow,
                job_id=job_id,
                action_id=action_id,
                caller_id=caller_id,
                tool_name=tool_name,
                operation="tool_call",
            )

        if scope_token is not None:
            if scope_token.tenant_id != tenant_id:
                self._deny_and_raise(
                    category=ErrorCategory.AUTHZ_DENIED,
                    error_code="TENANT_ISOLATION_VIOLATION",
                    guided_recovery="ScopeCapabilityToken tenant does not match request tenant.",
                    field_path="scope_token.tenant_id",
                    trace_id=resolved_trace,
                    span_id=resolved_span,
                    tenant_id=tenant_id,
                    workflow=workflow,
                    job_id=job_id,
                    action_id=action_id,
                    caller_id=caller_id,
                    tool_name=tool_name,
                    operation="tool_call",
                )

            if patient_id is not None and scope_token.patient_id != patient_id:
                self._deny_and_raise(
                    category=ErrorCategory.AUTHZ_DENIED,
                    error_code="SCOPE_TOKEN_PATIENT_MISMATCH",
                    guided_recovery="ScopeCapabilityToken patient scope does not match target patient.",
                    field_path="scope_token.patient_id",
                    trace_id=resolved_trace,
                    span_id=resolved_span,
                    tenant_id=tenant_id,
                    workflow=workflow,
                    job_id=job_id,
                    action_id=action_id,
                    caller_id=caller_id,
                    tool_name=tool_name,
                    operation="tool_call",
                )

            if actor_id is not None and scope_token.actor_id != actor_id:
                self._deny_and_raise(
                    category=ErrorCategory.AUTHZ_DENIED,
                    error_code="SCOPE_TOKEN_ACTOR_MISMATCH",
                    guided_recovery="ScopeCapabilityToken actor does not match authenticated actor.",
                    field_path="scope_token.actor_id",
                    trace_id=resolved_trace,
                    span_id=resolved_span,
                    tenant_id=tenant_id,
                    workflow=workflow,
                    job_id=job_id,
                    action_id=action_id,
                    caller_id=caller_id,
                    tool_name=tool_name,
                    operation="tool_call",
                )

            now_utc = ensure_utc(now) if now is not None else datetime.now(timezone.utc)
            try:
                token_valid = _verify_token_integrity(
                    self._token_verifier, scope_token, now_utc
                )
            except Exception:
                token_valid = False

            if not token_valid:
                self._deny_and_raise(
                    category=ErrorCategory.AUTHZ_DENIED,
                    error_code="INVALID_OR_EXPIRED_SCOPE_TOKEN",
                    guided_recovery="ScopeCapabilityToken failed integrity verification or is expired.",
                    field_path="scope_token",
                    trace_id=resolved_trace,
                    span_id=resolved_span,
                    tenant_id=tenant_id,
                    workflow=workflow,
                    job_id=job_id,
                    action_id=action_id,
                    caller_id=caller_id,
                    tool_name=tool_name,
                    operation="tool_call",
                )

        return PolicyDecision(
            action_id=action_id,
            allowed=True,
            caller_id=caller_id,
            tenant_id=tenant_id,
            tool_name=tool_name,
        )

    def after_tool_call(
        self,
        *,
        caller_id: str,
        tool_name: str,
        action_id: str,
        tenant_id: str = "tenant-unknown",
        workflow: str = "adk_workflow",
        job_id: str = "job-unknown",
        trace_id: str | None = None,
        span_id: str | None = None,
        outcome: str = "SUCCESS",
        latency_ms: int | float | None = None,
        payload_digest: str | None = None,
        error_category: ErrorCategory | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
        encounter_id: str | None = None,
    ) -> dict[str, Any]:
        """Emit paired zero-PHI ACTION_OUTCOME telemetry after tool execution."""
        resolved_trace = trace_id or uuid.uuid4().hex
        resolved_span = span_id or uuid.uuid4().hex[:16]
        severity = "ERROR" if outcome in {"ERROR", "FAILED", "DENIED"} else "INFO"
        return self._audit_logger.log_outcome(
            trace_id=resolved_trace,
            span_id=resolved_span,
            tenant_id=tenant_id,
            workflow=workflow,
            job_id=job_id,
            action_id=action_id,
            outcome=outcome,
            severity=severity,
            actor_id=caller_id,
            tool_name=tool_name,
            operation="tool_call",
            latency_ms=latency_ms,
            payload_digest=payload_digest,
            error_category=error_category,
            error_code=error_code,
            error_summary=error_summary,
            encounter_id=encounter_id,
        )

    def before_model_call(
        self,
        *,
        caller_id: str,
        model_id: str,
        prompt_text: str,
        has_untrusted_data: bool = False,
        untrusted_text: str | None = None,
        expected_labels: Iterable[str] | None = None,
        max_input_tokens: int | None = None,
        estimated_tokens: int | None = None,
        tenant_id: str = "tenant-unknown",
        workflow: str = "adk_workflow",
        job_id: str = "job-unknown",
        trace_id: str | None = None,
        span_id: str | None = None,
        encounter_id: str | None = None,
    ) -> PolicyDecision:
        """Enforce prompt token budget and untrusted-data delimiter integrity before model invocation."""
        resolved_trace = trace_id or uuid.uuid4().hex
        resolved_span = span_id or uuid.uuid4().hex[:16]
        budget = max_input_tokens if max_input_tokens is not None else self._max_prompt_tokens
        if budget <= 0:
            raise ValueError("max_input_tokens must be positive")

        tokens = (
            estimated_tokens
            if estimated_tokens is not None
            else _estimate_token_count(prompt_text)
        )

        intent_record = self._audit_logger.log_intent(
            trace_id=resolved_trace,
            span_id=resolved_span,
            tenant_id=tenant_id,
            workflow=workflow,
            job_id=job_id,
            actor_id=caller_id,
            target_system=model_id,
            encounter_id=encounter_id,
            operation="model_call",
        )
        action_id = intent_record["action_id"]

        if tokens > budget:
            self._deny_and_raise(
                category=ErrorCategory.VALIDATION_RECOVERABLE,
                error_code="PROMPT_TOKEN_BUDGET_EXCEEDED",
                guided_recovery=(
                    f"Prompt token estimate ({tokens}) exceeds allowed budget ({budget}); compact context."
                ),
                field_path="prompt_text",
                trace_id=resolved_trace,
                span_id=resolved_span,
                tenant_id=tenant_id,
                workflow=workflow,
                job_id=job_id,
                action_id=action_id,
                caller_id=caller_id,
                target_system=model_id,
                operation="model_call",
            )

        requires_delimiters = (
            has_untrusted_data
            or untrusted_text is not None
            or expected_labels is not None
            or has_untrusted_delimiter_markers(prompt_text)
        )
        if requires_delimiters and not verify_untrusted_delimiters(
            prompt_text, expected_labels=expected_labels
        ):
            self._deny_and_raise(
                category=ErrorCategory.VALIDATION_RECOVERABLE,
                error_code="UNTRUSTED_DELIMITER_MISSING_OR_MALFORMED",
                guided_recovery=(
                    "Wrap all untrusted clinical text using wrap_untrusted_clinical_data() before model invocation."
                ),
                field_path="prompt_text",
                trace_id=resolved_trace,
                span_id=resolved_span,
                tenant_id=tenant_id,
                workflow=workflow,
                job_id=job_id,
                action_id=action_id,
                caller_id=caller_id,
                target_system=model_id,
                operation="model_call",
            )

        return PolicyDecision(
            action_id=action_id,
            allowed=True,
            caller_id=caller_id,
            tenant_id=tenant_id,
            model_id=model_id,
            estimated_tokens=tokens,
        )


__all__ = [
    "CareThreadPolicyPlugin",
    "DEFAULT_CALLER_ALLOWLIST",
    "DEFAULT_TOOLS_REQUIRING_SCOPE_TOKEN",
    "PolicyDecision",
]
