"""The expansion sampler must hold durable costs before an actual SDK call."""

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from minidsh_rl.cost_store import CostStore
from minidsh_rl.priced_backend import PricedBackend


@dataclass
class Tokenizer:
    chat_template = "fixture"

    def get_vocab(self) -> dict[str, int]:
        return {"x": 1}

    def apply_chat_template(self, conversation: object, **kwargs: object) -> list[int]:
        assert conversation is not None
        assert kwargs["return_dict"] is False
        return [1, 2]


@dataclass
class Sampler:
    path: Path
    fail: bool
    calls: int = 0

    async def sample_async(self, **kwargs: object) -> object:
        assert kwargs["num_samples"] == 1
        state = json.loads(self.path.read_text())
        assert sum(c["status"] == "pending" for c in state["charges"].values()) == 1
        self.calls += 1
        if self.fail:
            raise TimeoutError("unknown submitted operation")
        return SimpleNamespace(
            input_tokens=2,
            output_tokens=1,
            sequences=[SimpleNamespace(tokens=[3], logprobs=[-0.2], text="OK", stop_reason="stop")],
        )


@pytest.mark.parametrize("fail", [False, True])
async def test_sampler_holds_unknown_usage_without_retry(tmp_path: Path, *, fail: bool) -> None:
    old = tmp_path / "old.json"
    old.write_text(
        json.dumps(
            {"schema_version": 1, "limit": "50", "spent": "31.38", "pending": {}, "settled": []}
        )
    )
    quote = tmp_path / "quote.json"
    quote.write_text("{}")
    path = tmp_path / "ledger.json"
    sampler = Sampler(path, fail)
    with CostStore(
        path, legacy=old, quote=quote, limit=Decimal("50"), uncertainty=Decimal("2")
    ) as store:
        backend = PricedBackend(sampler, Tokenizer(), store, "base")
        if fail:
            with pytest.raises(TimeoutError, match="unknown"):
                await backend.sample((1, 2), 32, 17)
        else:
            result = await backend.sample((1, 2), 32, 17)
            assert result.turn.completion == (3,)
        assert sampler.calls == 1
    state = json.loads(path.read_text())
    assert [c["status"] for c in state["charges"].values()] == ["pending" if fail else "estimated"]

    assert [c["amount"] for c in state["charges"].values()] == ["0.002"]
