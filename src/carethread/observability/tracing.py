"""OpenTelemetry W3C traceparent propagation and allowlisted span helpers."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

ALLOWED_SPAN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "action_id",
        "attempt",
        "component",
        "encounter_id",
        "error_category",
        "error_code",
        "hitl_required",
        "http.method",
        "http.status_code",
        "job_id",
        "job_state",
        "max_retries_remaining",
        "operation",
        "outcome",
        "purpose",
        "redaction_fail_closed",
        "resource_id",
        "retry_count",
        "retryable",
        "role",
        "schema_version",
        "stage",
        "status",
        "target_system",
        "tenant_id",
        "tool_name",
        "workflow",
    }
)

_PROPAGATOR = TraceContextTextMapPropagator()
_DEFAULT_TRACER_PROVIDER = TracerProvider()


def _filter_allowlisted_attrs(
    attrs: Mapping[str, str | int | bool] | None,
) -> dict[str, str | int | bool]:
    if not attrs:
        return {}
    filtered: dict[str, str | int | bool] = {}
    for key, value in attrs.items():
        if key in ALLOWED_SPAN_ATTRIBUTES and isinstance(value, (str, int, bool)):
            filtered[key] = value
    return filtered


class TracingContext:
    """Helpers for W3C traceparent propagation and Zero-PHI OpenTelemetry spans."""

    def __init__(
        self,
        tracer_name: str = "carethread",
        tracer_provider: TracerProvider | None = None,
    ) -> None:
        self._tracer_name = tracer_name
        if tracer_provider is not None:
            self._tracer_provider = tracer_provider
        else:
            global_provider = trace.get_tracer_provider()
            if isinstance(global_provider, trace.ProxyTracerProvider):
                self._tracer_provider = _DEFAULT_TRACER_PROVIDER
            else:
                self._tracer_provider = global_provider

    @staticmethod
    def inject_traceparent(headers: dict[str, str] | None = None) -> dict[str, str]:
        """Inject the active W3C traceparent header into a carrier dictionary."""
        carrier: dict[str, str] = dict(headers) if headers is not None else {}
        _PROPAGATOR.inject(carrier)
        return carrier

    @staticmethod
    def extract_traceparent(headers: Mapping[str, str]) -> Context:
        """Extract an OpenTelemetry Context from incoming W3C trace headers."""
        normalized_headers = {key.lower(): value for key, value in headers.items()}
        return _PROPAGATOR.extract(normalized_headers)

    @staticmethod
    def get_current_trace_id(context: Context | None = None) -> str:
        """Return the 32-char lowercase hex trace_id for the active or given context."""
        span = (
            trace.get_current_span(context)
            if context is not None
            else trace.get_current_span()
        )
        span_ctx = span.get_span_context()
        if not span_ctx.is_valid:
            return ""
        return format(span_ctx.trace_id, "032x")

    @staticmethod
    def get_current_span_id(context: Context | None = None) -> str:
        """Return the 16-char lowercase hex span_id for the active or given context."""
        span = (
            trace.get_current_span(context)
            if context is not None
            else trace.get_current_span()
        )
        span_ctx = span.get_span_context()
        if not span_ctx.is_valid:
            return ""
        return format(span_ctx.span_id, "016x")

    @contextmanager
    def start_span(
        self,
        name: str,
        allowlisted_attrs: Mapping[str, str | int | bool] | None = None,
        context: Context | None = None,
    ) -> Iterator[Span]:
        """Start an OpenTelemetry span populated only with allowlisted attributes."""
        tracer = self._tracer_provider.get_tracer(self._tracer_name)
        safe_attrs = _filter_allowlisted_attrs(allowlisted_attrs)
        parent_ctx = context if context is not None else otel_context.get_current()
        with tracer.start_as_current_span(
            name,
            context=parent_ctx,
            attributes=safe_attrs,
        ) as span:
            yield span


__all__ = [
    "ALLOWED_SPAN_ATTRIBUTES",
    "TracingContext",
]
