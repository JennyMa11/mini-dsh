"""OpenInference translation, redaction, and lifecycle of Phoenix spans."""

from __future__ import annotations

from typing import cast

import pytest
from openinference.semconv.trace import SpanAttributes
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from minidsh.adapters.phoenix import PhoenixTraceSubscriber
from minidsh.domain import Event, JsonValue, PhoenixConfig


def _event(name: str, data: dict[str, object] | None = None, *, step: int = 1) -> Event:
    return Event(name, "run-1", step, cast("dict[str, JsonValue]", data or {}))


def _subscriber(
    *, capture_content: bool = True
) -> tuple[PhoenixTraceSubscriber, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    subscriber = PhoenixTraceSubscriber(
        PhoenixConfig(capture_content=capture_content), provider=provider
    )
    return subscriber, exporter


async def _publish(subscriber: PhoenixTraceSubscriber, *events: Event) -> None:
    for event in events:
        await subscriber(event)


def _by_name(spans: tuple[ReadableSpan, ...], name: str) -> ReadableSpan:
    return next(span for span in spans if span.name == name)


def _full_run_events() -> tuple[Event, ...]:
    return (
        _event("run_started"),
        _event(
            "session_message_committed",
            {
                "sequence": 0,
                "event_name": "user_message",
                "message": {"role": "user", "content": "inspect the repository"},
            },
        ),
        _event(
            "context_built",
            {
                "strategy": "budget",
                "version": "1.1",
                "estimated_tokens": 120,
                "history_estimated_tokens": 520,
                "omitted_messages": 5,
                "metadata": {
                    "omitted_estimated_tokens": 400,
                    "summary_estimated_tokens": 0,
                },
            },
        ),
        _event(
            "model_call_started",
            {
                "messages": [
                    {"role": "system", "content": "You are a coding agent."},
                    {"role": "user", "content": "inspect the repository"},
                ],
                "tools": [{"name": "read_file", "version": "1.0.0"}],
            },
        ),
        _event(
            "model_call_completed",
            {
                "finish_reason": "tool_calls",
                "content": "",
                "tool_calls": [
                    {"call_id": "call-1", "name": "read_file", "arguments": {"path": "a.py"}}
                ],
                "tool_call_count": 1,
                "input_tokens": 100,
                "output_tokens": 20,
                "cached_input_tokens": 10,
                "usage_known": True,
                "provider_model": "deepseek-chat",
                "system_fingerprint": "fp",
                "format_retries": 0,
                "cost_usd": 0.001,
                "duration_ms": 50.0,
                "inference": None,
            },
        ),
        _event(
            "tool_call_started",
            {"tool": "read_file", "call_id": "call-1", "arguments": {"path": "a.py"}},
        ),
        _event(
            "tool_call_completed",
            {
                "tool": "read_file",
                "call_id": "call-1",
                "status": "succeeded",
                "duration_ms": 12.0,
                "truncated": True,
                "retryable": False,
                "attempt": 1,
                "result": {"content": "bounded", "truncated": True},
                "error": None,
                "artifacts": [],
            },
        ),
        _event(
            "model_call_started",
            {
                "messages": [{"role": "user", "content": "continue"}],
                "tools": [],
            },
            step=2,
        ),
        _event(
            "model_call_completed",
            {
                "finish_reason": "stop",
                "content": "done",
                "tool_calls": [],
                "tool_call_count": 0,
                "input_tokens": 30,
                "output_tokens": 5,
                "cached_input_tokens": 0,
                "usage_known": True,
                "provider_model": "deepseek-chat",
                "format_retries": 0,
                "cost_usd": 0.0002,
                "duration_ms": 10.0,
                "inference": None,
            },
            step=2,
        ),
        _event(
            "runtime_verification_completed",
            {
                "runtime_verification": "passed",
                "attempt": 1,
                "call_id": "call-2",
                "criterion_version": "v1",
                "command_sha256": "abc",
                "reason": "",
            },
        ),
        _event(
            "run_finished",
            {
                "status": "finished",
                "steps": 2,
                "tokens": 155,
                "cached_input_tokens": 10,
                "cost_usd": 0.0012,
                "tool_calls": 1,
                "unknown_usage_calls": 0,
                "duration_ms": 100.0,
                "latency": {
                    "model_ms": 60.0,
                    "tool_ms": 12.0,
                    "harness_ms": 28.0,
                    "total_ms": 100.0,
                    "model_calls": 2,
                    "tool_calls": 1,
                },
                "failure_reason": None,
            },
        ),
    )


@pytest.mark.asyncio
async def test_phoenix_translates_events_into_one_nested_openinference_trace() -> None:
    subscriber, exporter = _subscriber()
    await _publish(subscriber, *_full_run_events())

    spans = exporter.get_finished_spans()
    root = _by_name(spans, "agent.run")
    context = _by_name(spans, "context.budget")
    tool = _by_name(spans, "tool.read_file")
    llm_spans = [span for span in spans if span.name == "llm.complete"]

    assert root.parent is None
    assert context.parent is not None
    assert context.parent.span_id == root.context.span_id
    assert tool.parent is not None
    assert tool.parent.span_id == root.context.span_id
    assert all(
        span.parent is not None and span.parent.span_id == root.context.span_id
        for span in llm_spans
    )

    root_attributes = root.attributes or {}
    assert root_attributes[SpanAttributes.OPENINFERENCE_SPAN_KIND] == "AGENT"
    assert root_attributes[SpanAttributes.INPUT_VALUE] == "inspect the repository"
    assert root_attributes[SpanAttributes.OUTPUT_VALUE] == "done"
    assert root_attributes["harness.run.status"] == "finished"
    assert root_attributes["harness.verification.status"] == "passed"
    assert root_attributes["harness.latency.model_ms"] == 60.0
    assert root.status.status_code is StatusCode.OK

    context_attributes = context.attributes or {}
    assert context_attributes[SpanAttributes.OPENINFERENCE_SPAN_KIND] == "CHAIN"
    assert context_attributes["context.omitted_messages"] == 5
    assert context_attributes["context.truncated"] is True
    assert context_attributes["context.omitted_estimated_tokens"] == 400
    assert context_attributes["context.tokens_saved"] == 400
    assert {event.name for event in root.events} >= {
        "context_truncated",
        "runtime_verification_completed",
    }

    completed = llm_spans[0]
    completed_attributes = completed.attributes or {}
    assert completed_attributes[SpanAttributes.OPENINFERENCE_SPAN_KIND] == "LLM"
    assert completed_attributes[SpanAttributes.LLM_MODEL_NAME] == "deepseek-chat"
    assert completed_attributes[SpanAttributes.LLM_TOKEN_COUNT_PROMPT] == 100
    assert completed_attributes[SpanAttributes.LLM_TOKEN_COUNT_COMPLETION] == 20
    assert completed_attributes[SpanAttributes.LLM_TOKEN_COUNT_TOTAL] == 120
    assert completed_attributes[SpanAttributes.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ] == 10
    assert completed_attributes["llm.input_messages.0.message.role"] == "system"
    assert completed_attributes["llm.output_messages.0.message.role"] == "assistant"
    assert (
        completed_attributes["llm.output_messages.0.message.tool_calls.0.tool_call.function.name"]
        == "read_file"
    )

    tool_attributes = tool.attributes or {}
    assert tool_attributes[SpanAttributes.OPENINFERENCE_SPAN_KIND] == "TOOL"
    assert tool_attributes[SpanAttributes.TOOL_NAME] == "read_file"
    assert tool_attributes["harness.tool.status"] == "succeeded"
    assert tool_attributes["harness.tool.truncated"] is True


@pytest.mark.asyncio
async def test_phoenix_records_a_rejected_tool_call_that_never_started() -> None:
    subscriber, exporter = _subscriber()
    await _publish(
        subscriber,
        _event("run_started"),
        _event(
            "tool_call_completed",
            {
                "tool": "shell",
                "call_id": "denied-1",
                "status": "denied",
                "duration_ms": 5.0,
                "truncated": False,
                "retryable": False,
                "attempt": 1,
                "result": None,
                "error": {"code": "permission_denied", "message": "tool is not allowed"},
                "artifacts": [],
            },
        ),
        _event(
            "run_finished",
            {"status": "failed", "failure_reason": "permission_denied", "steps": 1},
        ),
    )

    root = _by_name(exporter.get_finished_spans(), "agent.run")
    tool = _by_name(exporter.get_finished_spans(), "tool.shell")
    assert tool.parent is not None
    assert tool.parent.span_id == root.context.span_id
    assert tool.status.status_code is StatusCode.ERROR
    assert (tool.end_time or 0) - (tool.start_time or 0) >= 4_000_000
    attributes = tool.attributes or {}
    assert attributes["harness.tool.status"] == "denied"
    assert attributes["exception.type"] == "permission_denied"
    assert "tool is not allowed" in str(attributes[SpanAttributes.OUTPUT_VALUE])


@pytest.mark.asyncio
async def test_phoenix_redacts_secrets_and_bounds_captured_content() -> None:
    subscriber, exporter = _subscriber()
    secret = "sk-live-secret"
    large = "x" * 60_000
    await _publish(
        subscriber,
        _event("run_started"),
        _event(
            "tool_call_started",
            {
                "tool": "shell",
                "call_id": "call-1",
                "arguments": {"command": "echo", "api_key": secret},
            },
        ),
        _event(
            "tool_call_completed",
            {
                "tool": "shell",
                "call_id": "call-1",
                "status": "succeeded",
                "duration_ms": 1.0,
                "truncated": True,
                "retryable": False,
                "attempt": 1,
                "result": {"stdout": large, "api_key": secret},
                "error": None,
                "artifacts": [],
            },
        ),
    )

    tool = _by_name(exporter.get_finished_spans(), "tool.shell")
    attributes = tool.attributes or {}
    rendered = str(attributes)
    assert secret not in rendered
    assert "[REDACTED]" in rendered
    output = str(attributes[SpanAttributes.OUTPUT_VALUE])
    assert large not in output
    assert "truncated by mini-dsh phoenix adapter" in output
    assert len(output) < len(large)


@pytest.mark.asyncio
async def test_phoenix_can_export_metadata_without_prompt_or_result_content() -> None:
    subscriber, exporter = _subscriber(capture_content=False)
    await _publish(subscriber, *_full_run_events())

    spans = exporter.get_finished_spans()
    root = _by_name(spans, "agent.run")
    context = _by_name(spans, "context.budget")
    llm = next(span for span in spans if span.name == "llm.complete")
    tool = _by_name(spans, "tool.read_file")
    for span in (root, llm, tool):
        attributes = span.attributes or {}
        assert SpanAttributes.INPUT_VALUE not in attributes
        assert SpanAttributes.OUTPUT_VALUE not in attributes
    assert "context.metadata" not in (context.attributes or {})
    assert (llm.attributes or {})[SpanAttributes.LLM_TOKEN_COUNT_TOTAL] == 120
    assert (root.attributes or {})["harness.run.status"] == "finished"


@pytest.mark.asyncio
async def test_phoenix_shutdown_closes_unfinished_spans() -> None:
    subscriber, exporter = _subscriber()
    await _publish(
        subscriber,
        _event("run_started"),
        _event("model_call_started", {"messages": [], "tools": []}),
    )

    subscriber.shutdown()
    spans = exporter.get_finished_spans()

    root = _by_name(spans, "agent.run")
    llm = _by_name(spans, "llm.complete")
    assert root.status.status_code is StatusCode.ERROR
    assert (root.attributes or {})["harness.run.status"] == "abandoned"
    assert llm.status.status_code is StatusCode.ERROR
    assert root.end_time is not None


def test_phoenix_config_normalizes_the_otlp_traces_endpoint() -> None:
    assert PhoenixConfig(endpoint="http://localhost:6006").traces_endpoint == (
        "http://localhost:6006/v1/traces"
    )
    assert PhoenixConfig(endpoint="https://phoenix.example/v1/traces").traces_endpoint == (
        "https://phoenix.example/v1/traces"
    )


@pytest.mark.parametrize(
    "endpoint",
    ["localhost:6006", "ftp://localhost:6006", "http://user:pass@localhost:6006"],
)
def test_phoenix_config_rejects_unsafe_or_ambiguous_endpoints(endpoint: str) -> None:
    with pytest.raises(ValueError, match="phoenix endpoint"):
        PhoenixConfig(endpoint=endpoint)


def test_phoenix_config_requires_a_bounded_project_name() -> None:
    with pytest.raises(ValueError, match="project name"):
        PhoenixConfig(project_name=" ")
    with pytest.raises(ValueError, match="project name"):
        PhoenixConfig(project_name="x" * 129)
