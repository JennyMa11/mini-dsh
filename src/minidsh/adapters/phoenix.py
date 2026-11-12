"""Export Mini-dsh runtime facts as OpenInference spans to Arize Phoenix.

The adapter is deliberately event-driven: the Agent Loop already publishes one
structured fact per model call, tool call, context build, verification attempt and
terminal state.  Translation therefore lives outside the loop and can be omitted
without changing execution.  Nothing here runs unless Phoenix tracing is explicitly
enabled in configuration.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, cast

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from minidsh.domain import Event, JsonValue, PhoenixConfig, redact_text, redact_value
from minidsh.domain.observability import AttributeValue

_SERVICE_NAME: Final = "service.name"
_PROJECT_NAME: Final = "openinference.project.name"
_MAX_CONTENT_CHARS: Final = 50_000
_TRUNCATION_SUFFIX: Final = "\n…[truncated by mini-dsh phoenix adapter]"
_JSON_MIME: Final = "application/json"
_TEXT_MIME: Final = "text/plain"
_ABANDONED: Final = "run ended without a terminal trace event"


def _json(value: object) -> str:
    return json.dumps(
        redact_value(value),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _bounded(value: str) -> str:
    return (
        value
        if len(value) <= _MAX_CONTENT_CHARS
        else value[:_MAX_CONTENT_CHARS] + _TRUNCATION_SUFFIX
    )


def _encoded(value: object) -> str:
    return _bounded(_json(value))


def _tool_label(name: object) -> str:
    raw = name if isinstance(name, str) else "unknown"
    return _bounded(redact_text(raw))[:128] or "unknown"


def _safe_name(prefix: str, name: object) -> str:
    raw = name if isinstance(name, str) else "unknown"
    cleaned = "".join(character for character in raw if character.isalnum() or character in "._-")
    return f"{prefix}.{cleaned[:80] or 'unknown'}"


def _as_dict(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _as_list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _as_text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


def _attribute(span: Span, name: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, (str, bool, int, float)):
        span.set_attribute(name, value)
    else:
        span.set_attribute(name, _encoded(value))


def _content(span: Span, name: str, value: str, mime_type: str) -> None:
    span.set_attribute(name, _bounded(redact_text(value)))
    span.set_attribute(
        SpanAttributes.OUTPUT_MIME_TYPE
        if name == SpanAttributes.OUTPUT_VALUE
        else SpanAttributes.INPUT_MIME_TYPE,
        mime_type,
    )


def _message_attributes(
    span: Span,
    prefix: str,
    messages: list[JsonValue],
    *,
    capture_content: bool,
) -> None:
    for index, raw in enumerate(messages):
        message = _as_dict(raw)
        base = f"{prefix}.{index}.message"
        role = _as_text(message.get("role"))
        if role:
            span.set_attribute(f"{base}.role", role)
        content = _as_text(message.get("content"))
        if capture_content and content:
            span.set_attribute(f"{base}.content", _bounded(redact_text(content)))
        for call_index, raw_call in enumerate(_as_list(message.get("tool_calls"))):
            call = _as_dict(raw_call)
            call_base = f"{base}.tool_calls.{call_index}.tool_call.function"
            name = _as_text(call.get("name"))
            if name:
                span.set_attribute(f"{call_base}.name", _tool_label(name))
            arguments = call.get("arguments")
            if arguments is not None:
                span.set_attribute(f"{call_base}.arguments", _encoded(arguments))


def _set_usage_attributes(span: Span, data: dict[str, JsonValue]) -> None:
    span.set_attribute("harness.usage_known", data.get("usage_known") is not False)
    if data.get("usage_known") is False:
        return
    prompt_tokens = data.get("input_tokens")
    completion_tokens = data.get("output_tokens")
    cached_tokens = data.get("cached_input_tokens")
    if isinstance(prompt_tokens, int):
        span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_PROMPT, prompt_tokens)
    if isinstance(completion_tokens, int):
        span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, completion_tokens)
    if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
        span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_TOTAL, prompt_tokens + completion_tokens)
    if isinstance(cached_tokens, int):
        span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ, cached_tokens)


def _nanoseconds(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)


def _start_time(event: Event, duration_ms: object) -> int:
    if isinstance(duration_ms, (int, float)) and duration_ms > 0:
        return _nanoseconds(event.occurred_at - timedelta(milliseconds=duration_ms))
    return _nanoseconds(event.occurred_at)


@dataclass(slots=True)
class _RunTrace:
    root: Span
    input_captured: bool = False
    last_answer: str = ""
    model_span: Span | None = None
    tool_spans: dict[str, Span] = field(default_factory=dict)
    test_exit_code: int | None = None


class PhoenixTraceSubscriber:
    """Translate runtime events into one OpenInference trace per agent run."""

    def __init__(
        self,
        config: PhoenixConfig,
        *,
        provider: TracerProvider | None = None,
        run_attributes: Mapping[str, AttributeValue] | None = None,
    ) -> None:
        self._config = config
        self._provider = provider if provider is not None else _provider(config)
        self._tracer: Tracer = self._provider.get_tracer("minidsh.phoenix")
        self._run_attributes = dict(run_attributes or {})
        self._runs: dict[str, _RunTrace] = {}
        self._closed = False

    async def __call__(self, event: Event) -> None:
        handler = cast("Callable[[Event], None] | None", getattr(self, f"_on_{event.name}", None))
        if handler is not None:
            handler(event)

    def shutdown(self) -> None:
        """Close open traces, then flush and stop the provider exactly once."""
        if self._closed:
            return
        self._closed = True
        for run in tuple(self._runs.values()):
            self._abandon(run)
        self._runs.clear()
        self._provider.shutdown()

    def _on_run_started(self, event: Event) -> None:
        attributes: dict[str, AttributeValue] = {
            SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.AGENT.value,
            "harness.run.id": event.run_id,
            "harness.schema_version": event.schema_version,
        }
        for key, value in self._run_attributes.items():
            attributes[f"harness.config.{key}"] = value
        root = self._tracer.start_span(
            "agent.run", start_time=_nanoseconds(event.occurred_at), attributes=attributes
        )
        self._runs[event.run_id] = _RunTrace(root)

    def _on_session_message_committed(self, event: Event) -> None:
        run = self._runs.get(event.run_id)
        if run is None:
            return
        message = _as_dict(event.data.get("message"))
        role = _as_text(message.get("role"))
        content = _as_text(message.get("content"))
        if role == "user" and not run.input_captured:
            run.input_captured = True
            if self._config.capture_content and content:
                _content(run.root, SpanAttributes.INPUT_VALUE, content, _TEXT_MIME)
        elif role == "assistant" and not _as_list(message.get("tool_calls")) and content:
            run.last_answer = content

    def _on_context_built(self, event: Event) -> None:
        run = self._runs.get(event.run_id)
        if run is None:
            return
        strategy = _as_text(event.data.get("strategy"))
        estimated_tokens = event.data.get("estimated_tokens")
        omitted = event.data.get("omitted_messages")
        history_tokens = event.data.get("history_estimated_tokens")
        metadata = _as_dict(event.data.get("metadata"))
        omitted_tokens = metadata.get("omitted_estimated_tokens")
        summary_tokens = metadata.get("summary_estimated_tokens")
        attributes: dict[str, AttributeValue] = {
            SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
            "context.strategy": strategy,
            "context.version": _as_text(event.data.get("version")),
            "harness.step_id": event.step_id,
        }
        if isinstance(estimated_tokens, int):
            attributes["context.estimated_tokens"] = estimated_tokens
        if isinstance(omitted, int):
            attributes["context.omitted_messages"] = omitted
            attributes["context.truncated"] = omitted > 0
        if isinstance(history_tokens, int):
            attributes["context.history_estimated_tokens"] = history_tokens
        if isinstance(omitted_tokens, int):
            attributes["context.omitted_estimated_tokens"] = omitted_tokens
        if isinstance(summary_tokens, int):
            attributes["context.summary_estimated_tokens"] = summary_tokens
        if isinstance(omitted_tokens, int):
            attributes["context.tokens_saved"] = max(
                0, omitted_tokens - (summary_tokens if isinstance(summary_tokens, int) else 0)
            )
        if metadata and self._config.capture_content:
            attributes["context.metadata"] = _encoded(metadata)
        span = self._tracer.start_span(
            _safe_name("context", strategy),
            context=trace.set_span_in_context(run.root),
            start_time=_nanoseconds(event.occurred_at),
            attributes=attributes,
        )
        span.end(end_time=_nanoseconds(event.occurred_at))
        if isinstance(omitted, int) and omitted > 0:
            run.root.add_event(
                "context_truncated",
                attributes={
                    "context.strategy": strategy,
                    "context.omitted_messages": omitted,
                    "context.omitted_estimated_tokens": (
                        omitted_tokens if isinstance(omitted_tokens, int) else 0
                    ),
                },
                timestamp=_nanoseconds(event.occurred_at),
            )

    def _on_model_call_started(self, event: Event) -> None:
        run = self._runs.get(event.run_id)
        if run is None:
            return
        if run.model_span is not None:
            run.model_span.set_status(Status(StatusCode.ERROR, "model call superseded"))
            run.model_span.end(end_time=_nanoseconds(event.occurred_at))
        messages = _as_list(event.data.get("messages"))
        tools = _as_list(event.data.get("tools"))
        attributes: dict[str, AttributeValue] = {
            SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.LLM.value,
            "harness.step_id": event.step_id,
            "harness.input_messages": len(messages),
            "harness.tools": _json([_as_dict(tool).get("name") for tool in tools]),
        }
        if self._config.capture_content:
            attributes[SpanAttributes.INPUT_VALUE] = _encoded(messages)
            attributes[SpanAttributes.INPUT_MIME_TYPE] = _JSON_MIME
        run.model_span = self._tracer.start_span(
            "llm.complete",
            context=trace.set_span_in_context(run.root),
            start_time=_nanoseconds(event.occurred_at),
            attributes=attributes,
        )
        if self._config.capture_content:
            _message_attributes(
                run.model_span,
                SpanAttributes.LLM_INPUT_MESSAGES,
                messages,
                capture_content=True,
            )

    def _on_model_call_completed(self, event: Event) -> None:
        run = self._runs.get(event.run_id)
        if run is None:
            return
        span = self._model_span(run, event)
        data = event.data
        provider_model = data.get("provider_model")
        if isinstance(provider_model, str) and provider_model:
            span.set_attribute(SpanAttributes.LLM_MODEL_NAME, provider_model)
        finish_reason = data.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            span.set_attribute(SpanAttributes.LLM_FINISH_REASON, finish_reason)
        _set_usage_attributes(span, data)
        _attribute(span, "harness.cost_usd", data.get("cost_usd"))
        _attribute(span, "harness.duration_ms", data.get("duration_ms"))
        _attribute(span, "harness.format_retries", data.get("format_retries"))
        _attribute(span, "harness.system_fingerprint", data.get("system_fingerprint"))
        inference = _as_dict(data.get("inference"))
        for key, value in inference.items():
            if isinstance(value, (int, float)):
                span.set_attribute(f"harness.inference.{key}", value)
        self._record_model_output(
            span, run, _as_text(data.get("content")), _as_list(data.get("tool_calls"))
        )
        span.end(end_time=_nanoseconds(event.occurred_at))
        run.model_span = None

    def _record_model_output(
        self, span: Span, run: _RunTrace, content: str, tool_calls: list[JsonValue]
    ) -> None:
        if self._config.capture_content:
            if tool_calls:
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, _encoded(tool_calls))
                span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, _JSON_MIME)
            elif content:
                _content(span, SpanAttributes.OUTPUT_VALUE, content, _TEXT_MIME)
        output_message: dict[str, JsonValue] = {"role": "assistant", "content": content}
        if tool_calls:
            output_message["tool_calls"] = tool_calls
        _message_attributes(
            span,
            SpanAttributes.LLM_OUTPUT_MESSAGES,
            [output_message],
            capture_content=self._config.capture_content,
        )
        span.set_attribute("harness.tool_call_count", len(tool_calls))
        if content:
            run.last_answer = content

    def _on_model_call_failed(self, event: Event) -> None:
        run = self._runs.get(event.run_id)
        if run is None:
            return
        span = self._model_span(run, event)
        data = event.data
        error = _as_text(data.get("error"))
        span.set_status(Status(StatusCode.ERROR, _bounded(redact_text(error))[:256]))
        span.set_attribute("exception.type", "ModelCallError")
        span.set_attribute("exception.message", _bounded(redact_text(error)))
        _set_usage_attributes(span, data)
        _attribute(span, "harness.cost_usd", data.get("cost_usd"))
        _attribute(span, "harness.duration_ms", data.get("duration_ms"))
        span.end(end_time=_nanoseconds(event.occurred_at))
        run.model_span = None

    def _on_tool_call_started(self, event: Event) -> None:
        run = self._runs.get(event.run_id)
        if run is None:
            return
        call_id = _as_text(event.data.get("call_id"))
        name = _as_text(event.data.get("tool"))
        arguments = event.data.get("arguments")
        attributes: dict[str, AttributeValue] = {
            SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.TOOL.value,
            SpanAttributes.TOOL_NAME: _tool_label(name),
            "tool.id": call_id,
            "harness.step_id": event.step_id,
        }
        if self._config.capture_content and arguments is not None:
            attributes[SpanAttributes.INPUT_VALUE] = _encoded(arguments)
            attributes[SpanAttributes.INPUT_MIME_TYPE] = _JSON_MIME
        span = self._tracer.start_span(
            _safe_name("tool", name),
            context=trace.set_span_in_context(run.root),
            start_time=_nanoseconds(event.occurred_at),
            attributes=attributes,
        )
        run.tool_spans[call_id] = span

    def _on_tool_call_completed(self, event: Event) -> None:
        run = self._runs.get(event.run_id)
        if run is None:
            return
        call_id = _as_text(event.data.get("call_id"))
        name = _as_text(event.data.get("tool"))
        span = run.tool_spans.pop(call_id, None)
        if span is None:
            span = self._tracer.start_span(
                _safe_name("tool", name),
                context=trace.set_span_in_context(run.root),
                start_time=_start_time(event, event.data.get("duration_ms")),
                attributes={
                    SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.TOOL.value,
                    SpanAttributes.TOOL_NAME: _tool_label(name),
                    "tool.id": call_id,
                },
            )
        data = event.data
        status = _as_text(data.get("status"))
        result = data.get("result")
        error = _as_dict(data.get("error"))
        attempts = data.get("attempt")
        span.set_attribute("harness.tool.status", status)
        span.set_attribute("harness.tool.truncated", data.get("truncated") is True)
        span.set_attribute("harness.tool.retryable", data.get("retryable") is True)
        if isinstance(attempts, int):
            span.set_attribute("harness.tool.attempts", attempts)
            span.set_attribute("harness.tool.retries", max(0, attempts - 1))
        _attribute(span, "harness.duration_ms", data.get("duration_ms"))
        _attribute(span, "harness.artifacts", data.get("artifacts"))
        if status != "succeeded":
            span.set_status(Status(StatusCode.ERROR, status or "tool_failed"))
            message = _as_text(error.get("message")) or status or "tool_failed"
            span.set_attribute("exception.type", _as_text(error.get("code")) or status)
            span.set_attribute("exception.message", _bounded(redact_text(message)))
        if self._config.capture_content:
            payload: dict[str, JsonValue] = {"result": result}
            if error:
                payload["error"] = error
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, _encoded(payload))
            span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, _JSON_MIME)
        if isinstance(result, dict):
            exit_code = result.get("exit_code")
            timed_out = result.get("timed_out")
            if isinstance(exit_code, int):
                span.set_attribute("harness.exit_code", exit_code)
                if name == "test":
                    run.test_exit_code = exit_code
                    run.root.set_attribute("harness.test.exit_code", exit_code)
            if isinstance(timed_out, bool):
                span.set_attribute("harness.timed_out", timed_out)
        span.end(end_time=_nanoseconds(event.occurred_at))

    def _on_runtime_verification_completed(self, event: Event) -> None:
        run = self._runs.get(event.run_id)
        if run is None:
            return
        data = event.data
        attributes: dict[str, AttributeValue] = {}
        for key, attribute in (
            ("runtime_verification", "harness.verification.status"),
            ("attempt", "harness.verification.attempt"),
            ("reason", "harness.verification.reason"),
            ("criterion_version", "harness.verification.criterion_version"),
            ("command_sha256", "harness.verification.command_sha256"),
        ):
            value = data.get(key)
            if isinstance(value, (str, int, float, bool)):
                attributes[attribute] = value
                run.root.set_attribute(attribute, value)
        run.root.add_event(
            "runtime_verification_completed",
            attributes=attributes,
            timestamp=_nanoseconds(event.occurred_at),
        )

    def _on_run_finished(self, event: Event) -> None:
        run = self._runs.pop(event.run_id, None)
        if run is None:
            return
        if run.model_span is not None:
            run.model_span.set_status(Status(StatusCode.ERROR, _ABANDONED))
            run.model_span.end(end_time=_nanoseconds(event.occurred_at))
            run.model_span = None
        for span in run.tool_spans.values():
            span.set_status(Status(StatusCode.ERROR, _ABANDONED))
            span.end(end_time=_nanoseconds(event.occurred_at))
        run.tool_spans.clear()
        data = event.data
        status = _as_text(data.get("status"))
        failure_reason = _as_text(data.get("failure_reason"))
        run.root.set_attribute("harness.run.status", status)
        for key in ("steps", "tokens", "cached_input_tokens", "cost_usd", "tool_calls"):
            _attribute(run.root, f"harness.{key}", data.get(key))
        _attribute(run.root, "harness.unknown_usage_calls", data.get("unknown_usage_calls"))
        _attribute(run.root, "harness.duration_ms", data.get("duration_ms"))
        _attribute(run.root, "harness.failure_reason", data.get("failure_reason"))
        for key, value in _as_dict(data.get("latency")).items():
            if isinstance(value, (int, float)):
                run.root.set_attribute(f"harness.latency.{key}", value)
        if run.test_exit_code is not None:
            run.root.set_attribute("harness.test.passed", run.test_exit_code == 0)
        if status == "finished":
            run.root.set_status(Status(StatusCode.OK))
            if self._config.capture_content and run.last_answer:
                _content(run.root, SpanAttributes.OUTPUT_VALUE, run.last_answer, _TEXT_MIME)
        else:
            run.root.set_status(Status(StatusCode.ERROR, status or "run_failed"))
            if self._config.capture_content:
                _content(
                    run.root,
                    SpanAttributes.OUTPUT_VALUE,
                    failure_reason or status or "run_failed",
                    _TEXT_MIME,
                )
        run.root.end(end_time=_nanoseconds(event.occurred_at))

    def _model_span(self, run: _RunTrace, event: Event) -> Span:
        if run.model_span is not None:
            return run.model_span
        return self._tracer.start_span(
            "llm.complete",
            context=trace.set_span_in_context(run.root),
            start_time=_start_time(event, event.data.get("duration_ms")),
            attributes={
                SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.LLM.value,
                "harness.step_id": event.step_id,
            },
        )

    def _abandon(self, run: _RunTrace) -> None:
        if run.model_span is not None:
            run.model_span.set_status(Status(StatusCode.ERROR, _ABANDONED))
            run.model_span.end()
        for span in run.tool_spans.values():
            span.set_status(Status(StatusCode.ERROR, _ABANDONED))
            span.end()
        run.root.set_attribute("harness.run.status", "abandoned")
        run.root.set_status(Status(StatusCode.ERROR, _ABANDONED))
        run.root.end()


def _provider(config: PhoenixConfig) -> TracerProvider:
    resource = Resource.create(
        {
            _SERVICE_NAME: "mini-dsh",
            _PROJECT_NAME: config.project_name,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=config.traces_endpoint))
    )
    return provider


__all__ = ["PhoenixTraceSubscriber"]
