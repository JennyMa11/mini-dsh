"""Provider-independent recording adapter; tool effects remain in ToolRuntime."""

import json
import re
from dataclasses import dataclass, field, replace
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from minidsh.domain import (
    ErrorInfo,
    Message,
    ModelCallError,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from minidsh_rl.contracts import Turn


class _ToolEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class Sample:
    """A single real sampler response, not a decoded and retokenized action."""

    turn: Turn
    text: str
    stop_reason: str


class SamplingBackend(Protocol):
    def render(self, request: ModelRequest) -> tuple[int, ...]: ...
    async def sample(self, prompt: tuple[int, ...], limit: int, seed: int) -> Sample: ...


class RecordingSink(Protocol):
    async def append(self, record: dict[str, object]) -> None: ...


def _native_envelope(encoded: str) -> str:
    matched = re.fullmatch(
        r"\s*<function=([a-zA-Z_][a-zA-Z_0-9]*)>(.*?)</function>\s*", encoded, re.DOTALL
    )
    if matched is None:
        raise ValueError("invalid tool function envelope")
    parameters = matched.group(2)
    native_arguments: dict[str, object] = {}
    position = 0
    for parameter in re.finditer(
        r"<parameter=([a-zA-Z_][a-zA-Z_0-9]*)>(.*?)</parameter>",
        parameters,
        re.DOTALL,
    ):
        if parameters[position : parameter.start()].strip():
            raise ValueError("invalid tool parameter envelope")
        key, value = parameter.group(1), parameter.group(2).strip()
        if key in native_arguments:
            raise ValueError("duplicate tool parameter")
        try:
            native_arguments[key] = json.loads(value)
        except ValueError:
            if value.startswith(("[", "{", '"')):
                raise ValueError("invalid tool parameter JSON") from None
            native_arguments[key] = value
        position = parameter.end()
    if parameters[position:].strip():
        raise ValueError("invalid trailing tool parameter")
    return json.dumps({"name": matched.group(1), "arguments": native_arguments})


def parse_output(text: str, call_id: str) -> ModelResponse:
    """Parse one Qwen tool envelope without fixing or resampling its contents."""
    if "<tool_call>" not in text:
        if "</tool_call>" in text:
            raise ValueError("invalid tool envelope")
        return ModelResponse(content=text)
    if text.count("<tool_call>") != 1 or text.count("</tool_call>") != 1:
        raise ValueError("one complete tool envelope required")
    prefix, rest = text.split("<tool_call>", 1)
    encoded, suffix = rest.split("</tool_call>", 1)
    if suffix.strip():
        raise ValueError("trailing text after tool envelope")
    if encoded.strip().startswith("<function="):
        encoded = _native_envelope(encoded)
    try:
        envelope = _ToolEnvelope.model_validate_json(encoded)
    except ValueError as error:
        raise ValueError("invalid tool JSON") from error
    # The JSON decoder produces only JSON values; the domain validates tool schema later.
    arguments = json.loads(json.dumps(envelope.arguments))
    return ModelResponse(
        content=prefix.strip(),
        finish_reason="tool_calls",
        tool_calls=(ToolCall(call_id, envelope.name, arguments),),
    )


@dataclass(slots=True)
class RecordingModel:
    """One instance per run, permanently bound to a frozen policy identity."""

    backend: SamplingBackend
    sink: RecordingSink
    policy_id: str
    run_id: str
    seed: int = 17
    max_output: int = 512
    context_limit: int = 4096
    generated_limit: int = 2048
    total_limit: int = 24000
    turns: list[Turn] = field(default_factory=list)
    consumed: int = 0
    generated: int = 0

    def count_tokens(self, messages: tuple[Message, ...], tools: tuple[ToolDefinition, ...]) -> int:
        """Exact adapter input count for the existing Loop request budget check."""
        return len(self.backend.render(ModelRequest(messages, tools)))

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Persist raw sample before returning any executable action to the Loop."""
        prompt = self.backend.render(request)
        limit = min(
            self.max_output,
            self.generated_limit - self.generated,
            self.context_limit - len(prompt),
            self.total_limit - self.consumed - len(prompt),
        )
        if limit <= 0:
            raise ModelCallError("experiment token budget exhausted", code="context_overflow")
        sample = await self.backend.sample(prompt, limit, self.seed + len(self.turns))
        turn = sample.turn
        if turn.prompt != prompt or len(turn.completion) > limit:
            raise ValueError("sampler returned unexpected tokens")
        call_index = len(self.turns)
        self.turns.append(turn)
        self.generated += len(turn.completion)
        self.consumed += len(prompt) + len(turn.completion)
        await self.sink.append(
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "call_index": call_index,
                "policy_id": self.policy_id,
                "prompt_token_ids": list(prompt),
                "completion_token_ids": list(turn.completion),
                "old_logprobs": list(turn.logprobs),
                "raw_text": sample.text,
                "stop_reason": sample.stop_reason,
                "seed": self.seed + call_index,
                "temperature": 1.0,
                "top_p": 1.0,
            }
        )
        usage = TokenUsage(input_tokens=len(prompt), output_tokens=len(turn.completion))
        try:
            response = parse_output(sample.text, f"{self.run_id}_{call_index}")
        except ValueError:
            response = ModelResponse(
                content=sample.text,
                finish_reason="invalid",
                recoverable_error=ErrorInfo("invalid_action", "invalid tool output"),
            )
        return replace(response, usage=usage, provider_model=self.policy_id)
