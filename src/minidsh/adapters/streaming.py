"""Bounded OpenAI-compatible SSE parsing and incremental credential redaction."""

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace

import httpx
from pydantic import BaseModel, Field, ValidationError

from minidsh.domain import ModelCallError, ModelResponse, TokenUsage, ToolCall, redact_text
from minidsh.domain.harness import StreamEvent

_MAX_BYTES = 1_048_576
_HELD_WORDS = 3


async def close_stream(stream: AsyncIterator[StreamEvent]) -> None:
    """Close optional async generators before a wrapper relinquishes ownership."""
    close = getattr(stream, "aclose", None)
    if callable(close):
        await close()


class IncrementalRedactor:
    """Hold unfinished words/assignments and enough suffix to redact split credentials."""

    def __init__(self, secret: str = "") -> None:
        self._secret = secret
        self._pending = ""

    def feed(self, text: str, *, final: bool = False) -> str:
        self._pending += text
        if final:
            cutoff = len(self._pending)
        else:
            cutoff = max(0, len(self._pending) - max(64, len(self._secret) + 32))
            # Never split the final assignment or an unfinished token.
            words = list(re.finditer(r"\S+", self._pending[:cutoff]))
            cutoff = words[-_HELD_WORDS].start() if len(words) >= _HELD_WORDS else 0
        value, self._pending = self._pending[:cutoff], self._pending[cutoff:]
        if self._secret:
            value = value.replace(self._secret, "[REDACTED]")
        return redact_text(value)


class _Function(BaseModel):
    name: str | None = None
    arguments: str | None = None


class _CallDelta(BaseModel):
    index: int = Field(ge=0, le=127)
    id: str | None = None
    function: _Function = Field(default_factory=_Function)


class _Delta(BaseModel):
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[_CallDelta] = Field(default_factory=list)


class _Choice(BaseModel):
    index: int = 0
    delta: _Delta = Field(default_factory=_Delta)
    finish_reason: str | None = None


class _Usage(BaseModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    prompt_cache_hit_tokens: int = Field(default=0, ge=0)
    prompt_tokens_details: dict[str, int] | None = None


class _Frame(BaseModel):
    model: str | None = None
    system_fingerprint: str | None = None
    choices: list[_Choice] = Field(default_factory=list)
    usage: _Usage | None = None


@dataclass(slots=True)
class _State:
    text: str = ""
    reasoning: str = ""
    finish: str | None = None
    usage: TokenUsage | None = None
    calls: dict[int, tuple[str, str, str]] = field(default_factory=dict)
    model: str | None = None
    fingerprint: str | None = None

    def consume(self, frame: _Frame) -> str:
        self.model = frame.model or self.model
        self.fingerprint = frame.system_fingerprint or self.fingerprint
        if frame.usage is not None:
            usage = frame.usage
            cached = usage.prompt_cache_hit_tokens
            if usage.prompt_tokens_details:
                cached = usage.prompt_tokens_details.get("cached_tokens", cached)
            self.usage = TokenUsage(usage.prompt_tokens, usage.completion_tokens, 0.0, cached)
        text = ""
        for choice in frame.choices:
            if choice.index != 0:
                continue
            delta = choice.delta
            text += delta.content or ""
            self.reasoning += delta.reasoning_content or ""
            if choice.finish_reason:
                self.finish = choice.finish_reason
            for call in delta.tool_calls:
                identity, name, arguments = self.calls.get(call.index, ("", "", ""))
                self.calls[call.index] = (
                    identity + (call.id or ""),
                    name + (call.function.name or ""),
                    arguments + (call.function.arguments or ""),
                )
        self.text += text
        return text

    def response(self) -> ModelResponse:
        if self.finish is None:
            raise ModelCallError(
                "stream ended before a finish reason", code="stream_incomplete", usage=self.usage
            )
        calls = []
        for _, (identity, name, raw) in sorted(self.calls.items()):
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ModelCallError(
                    "invalid streamed tool arguments",
                    code="invalid_tool_arguments",
                    usage=self.usage,
                ) from exc
            if not identity or not name or not isinstance(arguments, dict):
                raise ModelCallError(
                    "invalid streamed tool call", code="invalid_tool_arguments", usage=self.usage
                )
            calls.append(ToolCall(identity, name, arguments))
        return ModelResponse(
            self.text,
            tuple(calls),
            self.finish,
            self.usage or TokenUsage(),
            self.reasoning,
            usage_known=self.usage is not None,
            provider_model=self.model,
            system_fingerprint=self.fingerprint,
        )


async def stream_completion(
    *,
    endpoint: str,
    payload: dict[str, object],
    secret: str,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[StreamEvent]:
    """No retries after emitting content; close the HTTP connection on every exit."""
    state = _State()
    sanitizer = IncrementalRedactor(secret)
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    body = {**payload, "stream": True, "stream_options": {"include_usage": True}}
    size = 0
    done = False
    try:
        async with (
            httpx.AsyncClient(
                transport=transport, trust_env=False, timeout=timeout_seconds
            ) as client,
            client.stream("POST", endpoint, json=body, headers=headers) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                size += len(line.encode())
                if size > _MAX_BYTES:
                    raise ModelCallError(
                        "stream exceeded output limit", code="output_limit", usage=state.usage
                    )
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done = True
                    break
                if not data:
                    continue
                try:
                    frame = _Frame.model_validate_json(data)
                except ValidationError as exc:
                    raise ModelCallError(
                        "invalid stream frame", code="bad_response", usage=state.usage
                    ) from exc
                text = sanitizer.feed(state.consume(frame))
                if text:
                    yield StreamEvent(text=text)
        if not done:
            raise ModelCallError(
                "stream ended without DONE", code="stream_incomplete", usage=state.usage
            )
        tail = sanitizer.feed("", final=True)
        if tail:
            yield StreamEvent(text=tail)
        final = state.response()
        # Whole-response redaction also covers reasoning and tool arguments in the wrapper.
        content = final.content.replace(secret, "[REDACTED]") if secret else final.content
        yield StreamEvent(response=replace(final, content=redact_text(content)))
    except httpx.HTTPError as exc:
        code = "network_error"
        if isinstance(exc, httpx.HTTPStatusError):
            code = "auth_error" if exc.response.status_code in {401, 403} else "client_error"
        raise ModelCallError("stream transport failed", code=code, usage=state.usage) from None
