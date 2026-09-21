"""Scripted and OpenAI-compatible model adapters."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Sequence
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


class ModelAdapterError(RuntimeError):
    """Safe provider or scripted-model failure."""


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


class _ProviderUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class _ProviderResponse(BaseModel):
    choices: list[_ProviderChoice]
    usage: _ProviderUsage = Field(default_factory=_ProviderUsage)


class OpenAICompatibleModel:
    """Minimal Chat Completions adapter with explicit endpoint and credentials."""

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str,
        timeout_seconds: float = 120.0,
        temperature: float = 0.0,
    ) -> None:
        if not endpoint.startswith(("https://", "http://")):
            msg = "model endpoint must be an HTTP(S) URL"
            raise ValueError(msg)
        if not model or not api_key:
            msg = "model and api_key are required"
            raise ValueError(msg)
        self._endpoint = endpoint
        self._model = model
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._temperature = temperature

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Send a provider-compatible request on a worker thread."""
        return await asyncio.to_thread(self._complete_sync, request)

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
        provider_request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(provider_request, timeout=self._timeout) as response:
                raw = response.read()
            parsed = _ProviderResponse.model_validate_json(raw)
        except (OSError, urllib.error.URLError, ValidationError) as exc:
            msg = f"model request failed: {type(exc).__name__}"
            raise ModelAdapterError(msg) from exc
        if not parsed.choices:
            msg = "model response contained no choices"
            raise ModelAdapterError(msg)
        choice = parsed.choices[0]
        calls: list[ToolCall] = []
        for call in choice.message.tool_calls:
            try:
                arguments = json.loads(call.function.arguments)
            except json.JSONDecodeError as exc:
                msg = f"model returned invalid tool arguments for {call.function.name}"
                raise ModelAdapterError(msg) from exc
            if not isinstance(arguments, dict):
                msg = f"tool arguments for {call.function.name} must be an object"
                raise ModelAdapterError(msg)
            calls.append(ToolCall(call.id, call.function.name, arguments))
        return ModelResponse(
            choice.message.content or "",
            tuple(calls),
            choice.finish_reason,
            TokenUsage(parsed.usage.prompt_tokens, parsed.usage.completion_tokens),
        )

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
