"""SDK boundary fault tests, independent of network and model availability."""

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from minidsh_rl.budget import BudgetStore
from minidsh_rl.pytrio_backend import PyTrioBackend

from minidsh.domain import Message, ModelRequest, Role


class Tokenizer:
    chat_template = "frozen_template"

    def get_vocab(self) -> dict[str, int]:
        return {"a": 1}

    def apply_chat_template(  # noqa: PLR0913 - native tokenizer API
        self,
        conversation: list[dict[str, object]],
        *,
        tools: list[dict[str, object]],
        tokenize: bool,
        add_generation_prompt: bool,
        enable_thinking: bool,
        return_dict: bool,
    ) -> list[int]:
        assert conversation == [{"role": "user", "content": "task"}]
        assert tools == []
        assert tokenize
        assert add_generation_prompt
        assert not enable_thinking
        assert not return_dict
        return [1, 2]


@dataclass
class Sequence:
    tokens: list[int]
    logprobs: list[float]
    text: str | None = "done"
    stop_reason: str = "stop"


@dataclass
class Response:
    sequences: list[Sequence]
    input_tokens: int
    output_tokens: int


@dataclass
class Sampler:
    budget: BudgetStore
    mismatch: bool = False
    fail: bool = False

    async def sample_async(
        self, *, prompt: object, num_samples: int, sampling_params: object
    ) -> Response:
        assert len(self.budget.ledger.pending) == 1  # reservation predates RPC
        assert prompt is not None
        assert sampling_params is not None
        assert num_samples == 1
        if self.fail:
            raise TimeoutError("unknown RPC outcome")
        return Response([Sequence([3], [-0.2])], 999 if self.mismatch else 2, 1)


async def test_real_sdk_input_objects_and_reservation(tmp_path: Path) -> None:
    with BudgetStore(tmp_path / "budget.json", Decimal("50")) as budget:
        backend = PyTrioBackend(Sampler(budget), Tokenizer(), budget, "policy")
        prompt = backend.render(ModelRequest((Message(Role.USER, "task"),)))
        result = await backend.sample(prompt, 512, 17)
        assert result.turn.completion == (3,)
        assert not budget.ledger.pending
        assert budget.ledger.spent == Decimal(".02")
        assert len(backend.identity()["template_hash"]) == 64


@pytest.mark.parametrize(("mismatch", "fail"), [(True, False), (False, True)])
async def test_unknown_or_mismatched_usage_keeps_reservation(
    tmp_path: Path,
    *,
    mismatch: bool,
    fail: bool,
) -> None:
    with BudgetStore(tmp_path / "budget.json", Decimal("50")) as budget:
        backend = PyTrioBackend(Sampler(budget, mismatch, fail), Tokenizer(), budget, "policy")
        with pytest.raises((ValueError, TimeoutError), match=r"usage|unknown"):
            await backend.sample((1, 2), 512, 17)
        assert len(budget.ledger.pending) == 1
        assert budget.ledger.available == Decimal("49.98")


def test_runtime_feedback_keeps_constraints_and_renders_as_environment_input(
    tmp_path: Path,
) -> None:
    class FeedbackTokenizer(Tokenizer):
        def apply_chat_template(
            self, conversation: list[dict[str, object]], **_options: object
        ) -> list[int]:
            assert conversation == [
                {"role": "system", "content": "permanent constraints"},
                {"role": "assistant", "content": "invalid action"},
                {"role": "user", "content": "Runtime feedback: exact schema required"},
            ]
            return [1, 2]

    with BudgetStore(tmp_path / "budget.json", Decimal("50")) as budget:
        backend = PyTrioBackend(Sampler(budget), FeedbackTokenizer(), budget, "policy")
        assert backend.render(
            ModelRequest(
                (
                    Message(Role.SYSTEM, "permanent constraints"),
                    Message(Role.ASSISTANT, "invalid action"),
                    Message(Role.SYSTEM, "exact schema required"),
                )
            )
        ) == (1, 2)
