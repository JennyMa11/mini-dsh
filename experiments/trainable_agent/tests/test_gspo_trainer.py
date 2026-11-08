"""A custom-loss RPC reserves both passes and preserves unknown transactions."""

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

torch = pytest.importorskip("torch")

from minidsh_rl.contracts import Turn  # noqa: E402
from minidsh_rl.gspo import (  # noqa: E402
    GSPOConfig,
    LossFn,
    build_gspo_batch,
)
from minidsh_rl.gspo_trainer import GSPOTrainer  # noqa: E402
from minidsh_rl.reprice import milli_upper  # noqa: E402
from minidsh_rl.trainer_v2 import TrainerV2  # noqa: E402
from minidsh_rl.transaction import UpdateJournal  # noqa: E402
from pytrio.client.training import TrainingClient  # type: ignore[import-untyped]  # noqa: E402
from pytrio.types.forward.datum import Datum  # type: ignore[import-untyped]  # noqa: E402


@dataclass
class Budget:
    available: Decimal = Decimal("10")
    reserved: list[Decimal] = field(default_factory=list)
    pending: dict[str, Decimal] = field(default_factory=dict)

    def reserve(self, operation: str, maximum: Decimal) -> None:
        self.reserved.append(maximum)
        self.pending[operation] = maximum

    def settle(self, operation: str, _actual: Decimal) -> None:
        self.pending.pop(operation)


class Client:
    def forward_backward_custom(self, *_args: object) -> object:
        raise TimeoutError("unknown custom-loss submission")


def test_two_pass_quote_and_unknown_result_cannot_repeat(tmp_path: Path) -> None:
    budget = Budget()
    trainer = GSPOTrainer(
        TrainerV2(cast(TrainingClient, Client()), budget, tmp_path, "sum"),
        GSPOConfig(0.2, 0.2),
    )
    batch = build_gspo_batch(((Turn((1, 2), (3,), (-0.1,)),),), (1.0,))
    with pytest.raises(TimeoutError, match="unknown custom"):
        trainer.update(0, batch)
    assert budget.reserved == [2 * milli_upper(0, 0, batch.padded_tokens)]
    assert len(budget.pending) == 1
    assert UpdateJournal.load(tmp_path / "update_000.json").state == "backward_submitted"
    with pytest.raises(FileExistsError):
        trainer.update(0, batch)
    assert len(budget.reserved) == 1


def test_zero_group_skips_rpcs_but_keeps_journal(tmp_path: Path) -> None:
    budget = Budget()
    trainer = GSPOTrainer(
        TrainerV2(cast(TrainingClient, Client()), budget, tmp_path, "sum"), GSPOConfig()
    )
    batch = build_gspo_batch(((Turn((1,), (2,), (-0.1,)),),), (0.0,))
    assert trainer.update(0, batch)["updated"] is False
    assert budget.reserved == []
    assert json.loads((tmp_path / "update_000.json").read_text())["state"] == "skipped"


def test_custom_ce_sum_does_not_reapply_ppo_sample_mean_compensation(tmp_path: Path) -> None:
    class CapturingClient:
        scale: float = -1

        def forward_backward_custom(self, data: list[Datum], loss_fn: LossFn) -> object:
            values = [
                torch.tensor(d.loss_fn_inputs["logprobs"].data, requires_grad=True) for d in data
            ]
            _, metrics = loss_fn(data, values)
            self.scale = metrics["gspo/remote_scale"]
            raise TimeoutError("stop after capture")

    client = CapturingClient()
    budget = Budget()
    trainer = GSPOTrainer(
        TrainerV2(cast(TrainingClient, client), budget, tmp_path, "sum"), GSPOConfig(0.2, 0.2)
    )
    turn = Turn((1,), (2,), (-0.1,))
    batch = build_gspo_batch(((turn, turn),), (1.0,))
    with pytest.raises(TimeoutError, match="after capture"):
        trainer.update(0, batch)
    assert client.scale == 1.0
