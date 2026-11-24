"""ADK Tool Registry enforcing strict naming, docstring completeness, and policy-gated invocation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
import inspect
import re
import time
from typing import Any

from pydantic import ConfigDict, Field

from carethread.errors import ErrorCategory, GuidedErrorEnvelope, GuidedErrorException
from carethread.plugins.policy_plugin import CareThreadPolicyPlugin
from carethread.schemas import StrictModel
from carethread.security.authz import ScopeCapabilityToken

TOOL_NAME_PATTERN = re.compile(r"^[a-z]+_[a-z_]+$")

FORBIDDEN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "update_record",
        "update_data",
        "handle",
        "process",
        "do_action",
        "run_task",
        "execute",
    }
)

FORBIDDEN_GENERIC_VERBS: frozenset[str] = frozenset({"handle", "process"})

REQUIRED_DOCSTRING_SECTIONS: tuple[str, ...] = (
    "Args:",
    "Returns:",
    "Permissible Callers:",
    "Side Effects:",
)


class ToolRegistrationError(ValueError):
    """Raised when a tool violates naming, generic-verb, or docstring completeness rules."""


class RegisteredTool(StrictModel):
    """Metadata record for a validated, versioned ADK tool."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    schema_version: str = Field(default="1.0.0", min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    docstring: str = Field(min_length=1)
    permissible_callers: frozenset[str] = Field(default_factory=frozenset)
    side_effects: str = Field(min_length=1)
    func: Callable[..., Any] = Field(exclude=True)


def _extract_section_body(docstring: str, section_header: str) -> str:
    """Extract the non-empty body under a Google-style docstring section header."""
    lines = docstring.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == section_header:
            body_lines: list[str] = []
            for next_line in lines[idx + 1 :]:
                stripped = next_line.strip()
                if stripped in REQUIRED_DOCSTRING_SECTIONS or (
                    stripped.endswith(":") and not next_line.startswith((" ", "\t"))
                ):
                    break
                if stripped:
                    body_lines.append(stripped)
            return " ".join(body_lines).strip()
    return ""


def _parse_callers_from_section(section_body: str) -> frozenset[str]:
    tokens = [
        tok.strip(" ,.;`-*")
        for tok in re.split(r"[\s,]+", section_body)
        if tok.strip(" ,.;`-*")
    ]
    return frozenset(tok for tok in tokens if re.fullmatch(r"[a-z][a-z0-9_]+", tok))


def validate_tool_definition(
    func: Callable[..., Any],
    *,
    name: str | None = None,
) -> tuple[str, str, str, frozenset[str], str]:
    """Validate tool name, generic-verb ban, and Google-style docstring completeness."""
    if not callable(func):
        raise ToolRegistrationError("Registered tool must be callable.")

    tool_name = name if name is not None else getattr(func, "__name__", "")
    if not isinstance(tool_name, str) or not tool_name:
        raise ToolRegistrationError("Tool name must be a non-empty string.")

    if tool_name in FORBIDDEN_TOOL_NAMES:
        raise ToolRegistrationError(
            f"Tool name '{tool_name}' is forbidden; use a descriptive <verb>_<domain_noun> name."
        )

    if not TOOL_NAME_PATTERN.fullmatch(tool_name) or tool_name.endswith("_") or "__" in tool_name:
        raise ToolRegistrationError(
            f"Tool name '{tool_name}' must match ^[a-z]+_[a-z_]+$ without trailing or double underscores."
        )

    leading_verb = tool_name.split("_", 1)[0]
    if leading_verb in FORBIDDEN_GENERIC_VERBS:
        raise ToolRegistrationError(
            f"Tool verb '{leading_verb}' in '{tool_name}' is too generic; use a specific clinical verb."
        )

    doc = inspect.getdoc(func)
    if not doc or not doc.strip():
        raise ToolRegistrationError(
            f"Tool '{tool_name}' must provide a Google-style docstring with "
            f"{', '.join(REQUIRED_DOCSTRING_SECTIONS)}."
        )

    missing_sections: list[str] = []
    empty_sections: list[str] = []
    section_bodies: dict[str, str] = {}
    for header in REQUIRED_DOCSTRING_SECTIONS:
        if header not in doc:
            missing_sections.append(header)
        else:
            body = _extract_section_body(doc, header)
            if not body:
                empty_sections.append(header)
            section_bodies[header] = body

    if missing_sections or empty_sections:
        details: list[str] = []
        if missing_sections:
            details.append(f"missing sections: {', '.join(missing_sections)}")
        if empty_sections:
            details.append(f"empty sections: {', '.join(empty_sections)}")
        raise ToolRegistrationError(
            f"Tool '{tool_name}' docstring is incomplete ({'; '.join(details)})."
        )

    first_line = doc.splitlines()[0].strip()
    if not first_line or first_line in REQUIRED_DOCSTRING_SECTIONS:
        raise ToolRegistrationError(
            f"Tool '{tool_name}' docstring must begin with a concise summary line."
        )

    parsed_callers = _parse_callers_from_section(section_bodies["Permissible Callers:"])
    side_effects = section_bodies["Side Effects:"]
    return tool_name, first_line, doc, parsed_callers, side_effects


class ToolRegistry:
    """Registry enforcing CareThread ADK tool naming, docstring linting, and policy-gated invocation."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        func: Callable[..., Any],
        *,
        name: str | None = None,
        permissible_callers: Iterable[str] | None = None,
        schema_version: str = "1.0.0",
    ) -> RegisteredTool:
        """Validate and register a versioned ADK tool."""
        tool_name, summary, doc, doc_callers, side_effects = validate_tool_definition(
            func, name=name
        )
        if tool_name in self._tools:
            raise ToolRegistrationError(f"Tool '{tool_name}' is already registered.")

        resolved_callers = (
            frozenset(permissible_callers)
            if permissible_callers is not None
            else doc_callers
        )
        record = RegisteredTool(
            schema_version=schema_version,
            name=tool_name,
            description=summary,
            docstring=doc,
            permissible_callers=resolved_callers,
            side_effects=side_effects,
            func=func,
        )
        self._tools[tool_name] = record
        return record

    def get(self, name: str) -> RegisteredTool:
        """Return a registered tool by exact name."""
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' is not registered.")
        return self._tools[name]

    def list_tools(self) -> list[RegisteredTool]:
        """Return all registered tools in deterministic registration order."""
        return list(self._tools.values())

    def tool_names(self) -> list[str]:
        """Return all registered tool names."""
        return list(self._tools.keys())

    def invoke_with_policy(
        self,
        *,
        policy_plugin: CareThreadPolicyPlugin,
        caller_id: str,
        tool_name: str,
        tool_kwargs: dict[str, Any] | None = None,
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
    ) -> Any:
        """Route a tool invocation through CareThreadPolicyPlugin before and after execution."""
        decision = policy_plugin.before_tool_call(
            caller_id=caller_id,
            tool_name=tool_name,
            tenant_id=tenant_id,
            workflow=workflow,
            job_id=job_id,
            trace_id=trace_id,
            span_id=span_id,
            scope_token=scope_token,
            require_scope_token=require_scope_token,
            patient_id=patient_id,
            actor_id=actor_id,
            encounter_id=encounter_id,
            now=now,
        )

        registered = self.get(tool_name)
        start_ns = time.perf_counter_ns()
        try:
            result = registered.func(**(tool_kwargs or {}))
        except GuidedErrorException as exc:
            elapsed_ms = round((time.perf_counter_ns() - start_ns) / 1_000_000, 3)
            policy_plugin.after_tool_call(
                caller_id=caller_id,
                tool_name=tool_name,
                action_id=decision.action_id,
                tenant_id=tenant_id,
                workflow=workflow,
                job_id=job_id,
                trace_id=trace_id,
                span_id=span_id,
                outcome="ERROR",
                latency_ms=elapsed_ms,
                error_category=exc.envelope.category,
                error_code=exc.envelope.error_code,
                error_summary=exc.envelope.guided_recovery,
                encounter_id=encounter_id,
            )
            raise
        except Exception as exc:
            elapsed_ms = round((time.perf_counter_ns() - start_ns) / 1_000_000, 3)
            policy_plugin.after_tool_call(
                caller_id=caller_id,
                tool_name=tool_name,
                action_id=decision.action_id,
                tenant_id=tenant_id,
                workflow=workflow,
                job_id=job_id,
                trace_id=trace_id,
                span_id=span_id,
                outcome="ERROR",
                latency_ms=elapsed_ms,
                error_code=type(exc).__name__,
                error_summary=f"Tool '{tool_name}' raised {type(exc).__name__}.",
                encounter_id=encounter_id,
            )
            raise

        elapsed_ms = round((time.perf_counter_ns() - start_ns) / 1_000_000, 3)
        if isinstance(result, GuidedErrorEnvelope):
            policy_plugin.after_tool_call(
                caller_id=caller_id,
                tool_name=tool_name,
                action_id=decision.action_id,
                tenant_id=tenant_id,
                workflow=workflow,
                job_id=job_id,
                trace_id=trace_id,
                span_id=span_id,
                outcome="FAILED",
                latency_ms=elapsed_ms,
                error_category=result.category,
                error_code=result.error_code,
                error_summary=result.guided_recovery,
                encounter_id=encounter_id,
            )
        else:
            policy_plugin.after_tool_call(
                caller_id=caller_id,
                tool_name=tool_name,
                action_id=decision.action_id,
                tenant_id=tenant_id,
                workflow=workflow,
                job_id=job_id,
                trace_id=trace_id,
                span_id=span_id,
                outcome="SUCCESS",
                latency_ms=elapsed_ms,
                encounter_id=encounter_id,
            )
        return result


__all__ = [
    "FORBIDDEN_GENERIC_VERBS",
    "FORBIDDEN_TOOL_NAMES",
    "REQUIRED_DOCSTRING_SECTIONS",
    "RegisteredTool",
    "TOOL_NAME_PATTERN",
    "ToolRegistrationError",
    "ToolRegistry",
    "validate_tool_definition",
]
