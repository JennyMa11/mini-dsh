"""PyTRIO SDK boundary. No clients or RPCs are created at import time."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, cast
from uuid import uuid4

import pytrio as trio  # type: ignore[import-untyped]

from minidsh.domain import ModelRequest
from minidsh_rl.budget import cost_upper_bound
from minidsh_rl.contracts import Turn
from minidsh_rl.cost_store import CostStore
from minidsh_rl.model import Sample


class Tokenizer(Protocol):
    """Narrow tokenizer operations needed by the experiment adapter."""

    chat_template: str

    def get_vocab(self) -> dict[str, int]: ...
    def apply_chat_template(  # noqa: PLR0913 - native tokenizer API
        self,
        conversation: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        tokenize: bool,
        add_generation_prompt: bool,
        enable_thinking: bool,
        return_dict: bool,
    ) -> list[int]: ...


@dataclass(slots=True)
class ExpansionBackend:
    """A frozen SDK sampler, explicit tokenizer, and one shared spending lease."""

    sampler: object
    tokenizer: Tokenizer
    budget: CostStore
    policy_id: str
    evaluation_reserve: Decimal = Decimal("0")

    def identity(self) -> dict[str, str]:
        """Hash vocabulary and template to bind every sample to its renderer."""
        vocab = json.dumps(self.tokenizer.get_vocab(), sort_keys=True).encode()
        return {
            "policy_id": self.policy_id,
            "vocabulary_hash": hashlib.sha256(vocab).hexdigest(),
            "template_hash": hashlib.sha256(self.tokenizer.chat_template.encode()).hexdigest(),
            "enable_thinking": "false",
            "rendering_version": "qwen35_xml_feedback_v2",
        }

    def render(self, request: ModelRequest) -> tuple[int, ...]:
        """Render the exact post-Hook domain request with the native tool template."""
        messages: list[dict[str, object]] = []
        for message in request.messages:
            payload: dict[str, object] = {"role": message.role.value, "content": message.content}
            if message.role.value == "system" and messages:
                payload = {"role": "user", "content": "Runtime feedback: " + message.content}
            if message.tool_call_id:
                payload["tool_call_id"] = message.tool_call_id
            if message.tool_calls:
                payload["tool_calls"] = [
                    {
                        "type": "function",
                        "id": call.call_id,
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in message.tool_calls
                ]
            messages.append(payload)
        tools: list[dict[str, object]] = [
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
        tokens = self.tokenizer.apply_chat_template(
            messages,
            tools=tools,
            tokenize=True,
            add_generation_prompt=True,
            enable_thinking=False,
            return_dict=False,
        )
        if not tokens or any(type(token) is not int or token < 0 for token in tokens):
            raise ValueError("invalid rendered prompt tokens")
        return tuple(tokens)

    async def sample(self, prompt: tuple[int, ...], limit: int, seed: int) -> Sample:
        """Reserve worst-case spend before RPC; leave unknown outcomes reserved."""
        operation = "sample_" + uuid4().hex
        maximum = cost_upper_bound(len(prompt), limit, 0)
        if self.budget.available - maximum < self.evaluation_reserve:
            raise ValueError("reserved evaluation budget would be consumed")
        await asyncio.to_thread(self.budget.reserve, operation, maximum)
        # SDK lacks py.typed; keep its dynamic methods inside this one adapter boundary.
        sampler = cast("_SDKSampler", self.sampler)
        response = await sampler.sample_async(
            prompt=trio.ModelInput.from_ints(list(prompt)),
            num_samples=1,
            sampling_params=trio.SamplingParams(
                max_tokens=limit, temperature=1.0, top_p=1.0, top_k=-1, seed=seed
            ),
        )
        if len(response.sequences) != 1:
            raise ValueError("sampler returned unexpected number of sequences")
        sequence = response.sequences[0]
        turn = Turn(prompt, tuple(sequence.tokens), tuple(sequence.logprobs))
        if response.input_tokens != len(prompt) or response.output_tokens != len(turn.completion):
            raise ValueError("reported usage does not match actual tokens")
        # Settle at our conservative no-cache upper rate; bill reconciliation may be lower.
        await asyncio.to_thread(
            self.budget.settle, operation, cost_upper_bound(len(prompt), len(turn.completion), 0)
        )
        if sequence.text is None:
            raise ValueError("sampler omitted decoded text")
        return Sample(turn, sequence.text, sequence.stop_reason)


class _Sequence(Protocol):
    tokens: list[int]
    logprobs: list[float]
    text: str | None
    stop_reason: str


class _Response(Protocol):
    sequences: list[_Sequence]
    input_tokens: int
    output_tokens: int


class _SDKSampler(Protocol):
    async def sample_async(
        self, *, prompt: object, num_samples: int, sampling_params: object
    ) -> _Response: ...
