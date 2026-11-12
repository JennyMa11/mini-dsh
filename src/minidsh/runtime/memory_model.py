"""Reject registered forbidden material before model output becomes a durable fact."""

import json
from collections.abc import AsyncIterator
from dataclasses import asdict

from minidsh.domain import (
    ErrorInfo,
    JsonValue,
    Message,
    ModelCallError,
    ModelRequest,
    ModelResponse,
    ToolDefinition,
    estimate_tokens,
    redact_text,
)
from minidsh.domain.harness import StreamEvent
from minidsh.domain.memory import MemoryValidationError
from minidsh.domain.memory_content import MemoryContentPolicy
from minidsh.ports import Model, StreamingModel


class MemoryGuardedModel:
    """One response ingress guard, enabled only with explicit material fingerprints.

    Hash-only policy cannot decide whether an incremental prefix is a full match.
    With this guard, streaming display is buffered until the final response passes.
    Original usage is retained even when the entire action is rejected.
    """

    native_streaming = False

    def __init__(self, model: Model, forbidden_sha256: tuple[str, ...]) -> None:
        self._model = model
        self._policy = MemoryContentPolicy(forbidden_sha256)

    def _check(self, value: JsonValue) -> None:
        if isinstance(value, str):
            self._policy.check(value)
        elif isinstance(value, dict):
            self._policy.check(
                json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
            for key, item in value.items():
                self._policy.check(key)
                self._check(item)
        elif isinstance(value, (list, tuple)):
            self._policy.check(
                json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
            for item in value:
                self._check(item)

    def _response(self, response: ModelResponse) -> ModelResponse:
        try:
            self._check(asdict(response))
        except MemoryValidationError:
            return ModelResponse(
                usage=response.usage,
                usage_known=response.usage_known,
                format_retries=response.format_retries,
                inference=response.inference,
                recoverable_error=ErrorInfo("memory_forbidden_content", "memory_forbidden_content"),
            )
        return response

    def _error(self, exc: Exception) -> ModelCallError:
        # Provider error strings may contain the same material as a response.
        # Keep reported usage/classification; never retain untrusted error text.
        code = exc.code if isinstance(exc, ModelCallError) else "memory_model_failed"
        try:
            self._policy.check(code)
            code = redact_text(code)
        except MemoryValidationError:
            code = "memory_model_failed"
        return ModelCallError(
            "memory_model_failed",
            code=code,
            usage=exc.usage if isinstance(exc, ModelCallError) else None,
            retryable=exc.retryable if isinstance(exc, ModelCallError) else False,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            return self._response(await self._model.complete(request))
        except Exception as exc:
            raise self._error(exc) from None

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        if not isinstance(self._model, StreamingModel):
            yield StreamEvent(response=await self.complete(request))
            return
        stream = self._model.stream(request)
        try:
            async for item in stream:
                if item.response is not None:
                    yield StreamEvent(response=self._response(item.response))
        except Exception as exc:
            raise self._error(exc) from None
        finally:
            close = getattr(stream, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception as exc:
                    raise self._error(exc) from None

    def count_tokens(self, messages: tuple[Message, ...], tools: tuple[ToolDefinition, ...]) -> int:
        count = getattr(self._model, "count_tokens", None)
        if callable(count):
            result = count(messages, tools)
            if type(result) is not int or result < 0:
                raise ValueError("invalid_model_token_count")
            return result
        return estimate_tokens(messages)
