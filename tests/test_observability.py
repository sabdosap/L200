import ast
import json
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from carethread.errors import ErrorCategory, GuidedErrorEnvelope
from carethread.observability import (
    FailClosedRedactor,
    LocalRegexSdpClient,
    SdpClient,
    StructuredAuditLogger,
    TracingContext,
)


SYNTHETIC_PHI_TEXT = (
    "Validation failed for Patient Alex Rivera DOB 1980-05-14 "
    "MRN-999-001 audio gs://private-audio/enc1.wav"
)
SYNTHETIC_PHI_TOKENS = (
    "Alex Rivera",
    "Alex",
    "Rivera",
    "1980-05-14",
    "MRN-999-001",
    "gs://private-audio/enc1.wav",
)


class _BrokenSdpClient:
    """Test double implementing SdpClient that always fails."""

    def deidentify_text(self, text: str, info_types: tuple[str, ...]) -> str:
        raise RuntimeError("SDP unavailable")


def test_allowlisted_fields_pass_and_forbidden_keys_are_stripped():
    sink: list[dict] = []
    redactor = FailClosedRedactor(LocalRegexSdpClient())
    logger = StructuredAuditLogger(redactor=redactor, sink=sink)

    logger.log_event(
        severity="INFO",
        event_type="WORKFLOW_STEP",
        trace_id="0123456789abcdef0123456789abcdef",
        span_id="0123456789abcdef",
        tenant_id="tenant-clinic-a",
        workflow="soap_draft",
        job_id="job-123",
        outcome="SUCCESS",
        error_category=ErrorCategory.VALIDATION_RECOVERABLE,
        retryable=True,
        max_retries_remaining=2,
        # Forbidden PHI keys and arbitrary unknown keys must be stripped:
        raw_prompt="Patient Alex Rivera full prompt",
        transcript_text="Clinician spoke with Alex Rivera",
        audio_gcs_path="gs://private-audio/enc1.wav",
        patient_name="Alex Rivera",
        model_output='{"subjective": "Alex Rivera"}',
        unallowlisted_custom_field="should not appear",
    )

    assert len(sink) == 1
    record = sink[0]
    serialized = json.dumps(record)

    assert record["severity"] == "INFO"
    assert record["event_type"] == "WORKFLOW_STEP"
    assert record["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert record["span_id"] == "0123456789abcdef"
    assert record["tenant_id"] == "tenant-clinic-a"
    assert record["workflow"] == "soap_draft"
    assert record["job_id"] == "job-123"
    assert record["outcome"] == "SUCCESS"
    assert record["error_category"] == "VALIDATION_RECOVERABLE"
    assert record["retryable"] is True
    assert record["max_retries_remaining"] == 2

    for forbidden in (
        "raw_prompt",
        "transcript_text",
        "audio_gcs_path",
        "patient_name",
        "model_output",
        "unallowlisted_custom_field",
    ):
        assert forbidden not in record

    for phi in SYNTHETIC_PHI_TOKENS:
        assert phi not in serialized


def test_residual_error_summary_is_scrubbed_before_logging():
    sink: list[dict] = []
    sdp_client = LocalRegexSdpClient()
    assert isinstance(sdp_client, SdpClient)
    redactor = FailClosedRedactor(sdp_client)
    logger = StructuredAuditLogger(redactor=redactor, sink=sink)

    scrubbed, failed_closed = redactor.scrub_free_text(SYNTHETIC_PHI_TEXT)
    assert failed_closed is False
    assert scrubbed is not None
    for phi in SYNTHETIC_PHI_TOKENS:
        assert phi not in scrubbed
    assert redactor.sdp_unavailable_fail_closed_count == 0

    logger.log_event(
        severity="ERROR",
        event_type="VALIDATION_FAILED",
        trace_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        span_id="bbbbbbbbbbbbbbbb",
        tenant_id="tenant-clinic-a",
        workflow="soap_draft",
        job_id="job-456",
        error_category=ErrorCategory.VALIDATION_RECOVERABLE,
        error_summary=SYNTHETIC_PHI_TEXT,
    )

    assert len(sink) == 1
    record = sink[0]
    serialized = json.dumps(record)
    assert "error_summary" in record
    assert record["redaction_fail_closed"] is False
    for phi in SYNTHETIC_PHI_TOKENS:
        assert phi not in serialized


def test_fail_closed_sdp_outage_drops_phi_and_increments_counter():
    sink: list[dict] = []
    redactor = FailClosedRedactor(_BrokenSdpClient())
    logger = StructuredAuditLogger(redactor=redactor, sink=sink)

    scrubbed, failed_closed = redactor.scrub_free_text(SYNTHETIC_PHI_TEXT)
    assert (scrubbed, failed_closed) == (None, True)
    assert redactor.sdp_unavailable_fail_closed_count == 1

    logger.log_event(
        severity="ERROR",
        event_type="VALIDATION_FAILED",
        trace_id="cccccccccccccccccccccccccccccccc",
        span_id="dddddddddddddddd",
        tenant_id="tenant-clinic-b",
        workflow="continuity_brief",
        job_id="job-789",
        error_summary=SYNTHETIC_PHI_TEXT,
        redaction_fail_closed=False,
    )

    assert redactor.sdp_unavailable_fail_closed_count == 2
    assert len(sink) == 1
    record = sink[0]
    serialized = json.dumps(record)

    assert record.get("error_summary") is None
    assert record["redaction_fail_closed"] is True
    assert record["error_category"] == ErrorCategory.SDP_UNAVAILABLE_FAIL_CLOSED.value
    for phi in SYNTHETIC_PHI_TOKENS:
        assert phi not in serialized


def test_log_intent_and_log_outcome_emit_paired_records():
    sink: list[dict] = []
    redactor = FailClosedRedactor(LocalRegexSdpClient())
    logger = StructuredAuditLogger(redactor=redactor, sink=sink)

    trace_id = "11111111111111112222222222222222"
    intent_record = logger.log_intent(
        trace_id=trace_id,
        span_id="3333333333333333",
        tenant_id="tenant-clinic-a",
        workflow="insurer_packet",
        job_id="job-900",
        operation="export_fhir_bundle",
        actor_id="clinician-42",
    )
    action_id = intent_record["action_id"]
    assert isinstance(action_id, str) and len(action_id) > 0

    outcome_record = logger.log_outcome(
        trace_id=trace_id,
        span_id="4444444444444444",
        tenant_id="tenant-clinic-a",
        workflow="insurer_packet",
        job_id="job-900",
        action_id=action_id,
        outcome="SUCCESS",
        duration_ms=42,
    )

    assert len(sink) == 2
    assert sink[0]["event_type"] == "ACTION_INTENT"
    assert sink[1]["event_type"] == "ACTION_OUTCOME"
    assert sink[0]["action_id"] == sink[1]["action_id"] == action_id
    assert sink[0]["trace_id"] == sink[1]["trace_id"] == trace_id
    assert sink[1]["outcome"] == "SUCCESS"
    assert sink[1]["duration_ms"] == 42


def test_log_guided_error_consumes_guided_error_envelope():
    sink: list[dict] = []
    redactor = FailClosedRedactor(LocalRegexSdpClient())
    logger = StructuredAuditLogger(redactor=redactor, sink=sink)

    envelope = GuidedErrorEnvelope(
        error_code="STALE_CONTINUITY_GRAPH",
        category=ErrorCategory.STALE_GRAPH_DETECTED,
        retryable=True,
        max_retries_remaining=1,
        field_path="encounter_graph.version",
        guided_recovery="Re-fetch encounter graph and retry synthesis.",
        trace_id="55555555555555556666666666666666",
    )

    record = logger.log_guided_error(
        envelope=envelope,
        span_id="7777777777777777",
        tenant_id="tenant-clinic-a",
        workflow="continuity_brief",
        job_id="job-901",
    )

    assert record["severity"] == "ERROR"
    assert record["error_code"] == "STALE_CONTINUITY_GRAPH"
    assert record["error_category"] == "STALE_GRAPH_DETECTED"
    assert record["retryable"] is True
    assert record["max_retries_remaining"] == 1
    assert record["field_path"] == "encounter_graph.version"
    assert record["trace_id"] == envelope.trace_id


def test_traceparent_propagation_and_span_attribute_allowlisting():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing = TracingContext(tracer_provider=provider)

    with tracing.start_span(
        "enqueue_cloud_task",
        allowlisted_attrs={
            "tenant_id": "tenant-clinic-a",
            "workflow": "soap_draft",
            "job_id": "job-555",
            "retry_count": 0,
            "hitl_required": True,
            "patient_name": "Alex Rivera",
            "raw_prompt": "secret phi",
        },
    ) as parent_span:
        parent_trace_id = parent_span.get_span_context().trace_id
        assert parent_trace_id != 0
        headers = tracing.inject_traceparent()

    assert "traceparent" in headers
    simulated_cloud_tasks_headers = {"Traceparent": headers["traceparent"]}
    extracted_ctx = tracing.extract_traceparent(simulated_cloud_tasks_headers)
    extracted_trace_id = trace.get_current_span(extracted_ctx).get_span_context().trace_id
    assert extracted_trace_id == parent_trace_id

    with tracing.start_span(
        "execute_cloud_task",
        allowlisted_attrs={"tenant_id": "tenant-clinic-a", "job_id": "job-555"},
        context=extracted_ctx,
    ) as child_span:
        assert child_span.get_span_context().trace_id == parent_trace_id

    finished_spans = exporter.get_finished_spans()
    assert len(finished_spans) == 2
    enqueue_attrs = dict(finished_spans[0].attributes or {})
    assert enqueue_attrs["tenant_id"] == "tenant-clinic-a"
    assert enqueue_attrs["workflow"] == "soap_draft"
    assert enqueue_attrs["job_id"] == "job-555"
    assert enqueue_attrs["retry_count"] == 0
    assert enqueue_attrs["hitl_required"] is True
    assert "patient_name" not in enqueue_attrs
    assert "raw_prompt" not in enqueue_attrs


def test_no_print_statements_in_carethread_package():
    src_root = Path(__file__).resolve().parents[1] / "src" / "carethread"
    for py_file in src_root.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "print", f"Forbidden print() call found in {py_file}"
