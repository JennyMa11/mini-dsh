"""Scripted and OpenAI-compatible model adapters."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from minidsh.domain import (
    JsonValue,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    TokenUsage,
    ToolCall,
    estimate_tokens,
)

_AUTH_STATUS = frozenset({401, 403})
_RATE_LIMIT_STATUS = 429
_TRANSIENT_STATUS = frozenset({408, 409, 425, 500, 502, 503, 504})


class ModelAdapterError(RuntimeError):
    """Safe provider or scripted-model failure.

    ``code`` and ``retryable`` exist so the loop and the failure taxonomy can tell a
    transient provider hiccup (``rate_limited``, ``server_error``, ``timeout``) apart
    from a configuration or capability problem (``missing_credentials``,
    ``bad_response``, ``invalid_tool_arguments``) without parsing message text.
    """

    def __init__(self, message: str, *, code: str = "model_error", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(slots=True)
class ScriptedModel:
    """Deterministic model used by tests, demos, and reproducible benchmarks.

    ``input_tokens`` is optional in a script file. When it is omitted the adapter
    measures the tokens of the context it actually received, which is what makes a
    context-strategy ablation observable instead of flat.
    """

    responses: list[ModelResponse]
    requests: list[ModelRequest]
    input_token_overrides: list[int | None]
    measure_input_tokens: bool

    def __init__(
        self,
        responses: list[ModelResponse],
        *,
        measure_input_tokens: bool = False,
        input_token_overrides: Sequence[int | None] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.requests = []
        self.measure_input_tokens = measure_input_tokens
        declared = list(input_token_overrides) if input_token_overrides is not None else []
        self.input_token_overrides = declared + [None] * (len(self.responses) - len(declared))

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Return the next fixed response, measuring context tokens when asked."""
        self.requests.append(request)
        if not self.responses:
            msg = "scripted model has no responses remaining"
            raise ModelAdapterError(msg)
        index = len(self.requests) - 1
        response = self.responses.pop(0)
        if not self.measure_input_tokens:
            return response
        declared = (
            self.input_token_overrides[index] if index < len(self.input_token_overrides) else None
        )
        measured = declared if declared is not None else estimate_tokens(request.messages)
        return replace(response, usage=TokenUsage(measured, response.usage.output_tokens))

    @classmethod
    def from_file(cls, path: Path) -> ScriptedModel:
        """Load a JSON array of provider-neutral responses."""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            parsed = _ScriptFile.model_validate({"responses": payload})
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            msg = f"invalid scripted model file: {path}"
            raise ModelAdapterError(msg) from exc
        responses = [
            ModelResponse(
                content=item.content,
                tool_calls=tuple(
                    ToolCall(call.call_id, call.name, call.arguments) for call in item.tool_calls
                ),
                finish_reason=item.finish_reason,
                usage=TokenUsage(item.input_tokens or 0, item.output_tokens),
            )
            for item in parsed.responses
        ]
        return cls(
            responses,
            measure_input_tokens=True,
            input_token_overrides=[item.input_tokens for item in parsed.responses],
        )


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ScriptCall(_StrictModel):
    call_id: str
    name: str
    arguments: dict[str, JsonValue]


class _ScriptResponse(_StrictModel):
    content: str = ""
    tool_calls: list[_ScriptCall] = Field(default_factory=list)
    finish_reason: str = "stop"
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class _ScriptFile(_StrictModel):
    responses: list[_ScriptResponse]


class _ProviderFunction(BaseModel):
    name: str
    arguments: str


class _ProviderCall(BaseModel):
    id: str
    function: _ProviderFunction


class _ProviderMessage(BaseModel):
    content: str | None = None
    tool_calls: list[_ProviderCall] = Field(default_factory=list)


class _ProviderChoice(BaseModel):
    message: _ProviderMessage
    finish_reason: str = "stop"


class _ProviderPromptDetails(BaseModel):
    cached_tokens: int = 0


class _ProviderUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_details: _ProviderPromptDetails | None = None

    @property
    def cached_tokens(self) -> int:
        """Return the prompt-cache hit count, which providers nest one level down."""
        details = self.prompt_tokens_details
        return details.cached_tokens if details is not None else 0


class _ProviderResponse(BaseModel):
    choices: list[_ProviderChoice]
    usage: _ProviderUsage = Field(default_factory=_ProviderUsage)


class OpenAICompatibleModel:
    """Chat Completions adapter with explicit endpoint, credentials, and retry policy.

    Retrying a model call is safe because a completion has no side effect on the
    workspace: a duplicate request costs tokens but cannot corrupt state. That is why
    this adapter retries while the tool runtime deliberately does not retry tools that
    declare side effects.
    """

    def __init__(  # noqa: PLR0913 - explicit transport policy is the point of this adapter
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 120.0,
        temperature: float = 0.0,
        max_attempts: int = 4,
        backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 8.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if not endpoint.startswith(("https://", "http://")):
            msg = "model endpoint must be an HTTP(S) URL"
            raise ValueError(msg)
        if not model:
            msg = "model is required"
            raise ValueError(msg)
        if max_attempts < 1:
            msg = "max_attempts must be at least 1"
            raise ValueError(msg)
        self._endpoint = endpoint
        self._model = model
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._temperature = temperature
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._sleep = sleep or asyncio.sleep

    @property
    def model(self) -> str:
        """Return the pinned model name recorded in reports."""
        return self._model

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Send one request, retrying only explicitly transient failures."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                return await asyncio.to_thread(self._complete_sync, request)
            except ModelAdapterError as exc:
                if not exc.retryable or attempt == self._max_attempts:
                    raise
                await self._sleep(self._backoff(attempt))
        raise AssertionError("unreachable model retry state")

    def _backoff(self, attempt: int) -> float:
        """Return capped exponential backoff for the given 1-based attempt."""
        growth = self._backoff_seconds * float(2 ** (attempt - 1))
        return float(min(growth, self._max_backoff_seconds))

    def _complete_sync(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, object] = {
            "model": self._model,
            "temperature": self._temperature,
            "messages": [self._message_payload(message) for message in request.messages],
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        provider_request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(provider_request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except TimeoutError as exc:
            raise ModelAdapterError(
                "model request timed out", code="timeout", retryable=True
            ) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                raise ModelAdapterError(
                    "model request timed out", code="timeout", retryable=True
                ) from exc
            raise ModelAdapterError(
                "model request failed: network error", code="network_error", retryable=True
            ) from exc
        except OSError as exc:
            raise ModelAdapterError(
                f"model request failed: {type(exc).__name__}", code="network_error", retryable=True
            ) from exc
        try:
            parsed = _ProviderResponse.model_validate_json(raw)
        except ValidationError as exc:
            raise ModelAdapterError(
                "model returned an unparseable response", code="bad_response"
            ) from exc
        if not parsed.choices:
            raise ModelAdapterError("model response contained no choices", code="bad_response")
        choice = parsed.choices[0]
        calls: list[ToolCall] = []
        for call in choice.message.tool_calls:
            try:
                arguments = json.loads(call.function.arguments)
            except json.JSONDecodeError as exc:
                raise ModelAdapterError(
                    f"model returned invalid tool arguments for {call.function.name}",
                    code="invalid_tool_arguments",
                ) from exc
            if not isinstance(arguments, dict):
                raise ModelAdapterError(
                    f"tool arguments for {call.function.name} must be an object",
                    code="invalid_tool_arguments",
                )
            calls.append(ToolCall(call.id, call.function.name, arguments))
        return ModelResponse(
            choice.message.content or "",
            tuple(calls),
            choice.finish_reason,
            TokenUsage(
                parsed.usage.prompt_tokens,
                parsed.usage.completion_tokens,
                0.0,
                parsed.usage.cached_tokens,
            ),
        )

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError) -> ModelAdapterError:
        """Classify one HTTP failure into a safe, actionable adapter error."""
        status = exc.code
        if status in _AUTH_STATUS:
            return ModelAdapterError(
                f"provider rejected the credential (HTTP {status})", code="auth_error"
            )
        if status == _RATE_LIMIT_STATUS:
            return ModelAdapterError(
                "provider rate limit reached (HTTP 429)", code="rate_limited", retryable=True
            )
        if status in _TRANSIENT_STATUS:
            return ModelAdapterError(
                f"provider returned a transient error (HTTP {status})",
                code="server_error",
                retryable=True,
            )
        return ModelAdapterError(f"provider returned HTTP {status}", code="client_error")

    @staticmethod
    def _message_payload(message: Message) -> dict[str, object]:
        payload: dict[str, object] = {"role": message.role.value, "content": message.content}
        if message.name is not None:
            payload["name"] = message.name
        if message.role is Role.TOOL and message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in message.tool_calls
            ]
        return payload
