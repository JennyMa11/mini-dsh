"""The new ledger must preserve training transaction and zero-signal semantics."""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest
from minidsh_rl.contracts import Turn
from minidsh_rl.cost_store import CostStore
from minidsh_rl.datum import build_batch
from minidsh_rl.trainer_v2 import TrainerV2
from minidsh_rl.transaction import UpdateJournal


@dataclass
class Tensor:
    data: tuple[float, ...] = (0.0, -0.1)


@dataclass
class Response:
    metrics: dict[str, float] = field(default_factory=lambda: {"loss_sum": 0.0})
    loss_fn_outputs: list[dict[str, Tensor]] = field(
        default_factory=lambda: [{"logprobs": Tensor()}]
    )

    def model_dump_json(self, *, indent: int) -> str:
        assert indent == 2
        return '{"metrics":{"loss_sum":0.0}}'


@dataclass
class Future:
    fail: bool = False

    def result(self) -> Response:
        if self.fail:
            raise TimeoutError("unknown optimizer result")
        return Response()


@dataclass
class Client:
    fail: bool = False
    calls: int = 0

    def forward_backward(self, _data: list[object], **_kwargs: object) -> Future:
        self.calls += 1
        return Future()

    def optim_step(self, _parameters: object) -> Future:
        self.calls += 1
        return Future(self.fail)


@pytest.fixture
def budget(tmp_path: Path) -> Iterator[CostStore]:
    legacy = tmp_path / "old.json"
    legacy.write_text(
        json.dumps({"schema_version": 1, "limit": "50", "spent": "0", "pending": {}, "settled": []})
    )
    quote = tmp_path / "quote.json"
    quote.write_text("{}")
    with CostStore(
        tmp_path / "budget.json",
        legacy=legacy,
        quote=quote,
        limit=Decimal("50"),
        uncertainty=Decimal("0"),
    ) as store:
        yield store


def test_unknown_optimizer_is_not_replayed(tmp_path: Path, budget: CostStore) -> None:
    batch = build_batch(((Turn((1, 2), (3,), (-0.1,)),),), (1.0,))
    client = Client(fail=True)
    trainer = TrainerV2(client, budget, tmp_path, "sample_mean", Decimal("5"))
    with pytest.raises(TimeoutError, match="unknown optimizer"):
        trainer.update(0, batch)
    assert UpdateJournal.load(tmp_path / "update_000.json").state == "optim_submitted"
    with pytest.raises(FileExistsError):
        trainer.update(0, batch)
    assert client.calls == 2
    state = json.loads(budget.path.read_text())
    assert sum(c["status"] == "pending" for c in state["charges"].values()) == 1


def test_all_equal_rewards_skip_training_and_reservations(
    tmp_path: Path, budget: CostStore
) -> None:
    batch = build_batch(((Turn((1, 2), (3,), (-0.1,)),),), (0.0,))
    client = Client()
    result = TrainerV2(client, budget, tmp_path, "sample_mean", Decimal("5")).update(0, batch)
    assert result["updated"] is False
    assert client.calls == 0
    assert budget.available == Decimal("50")


def test_same_basename_in_different_experiments_has_distinct_operations(
    tmp_path: Path, budget: CostStore
) -> None:
    first = TrainerV2(Client(), budget, tmp_path / "old" / "smoke", "sample_mean")
    second = TrainerV2(Client(), budget, tmp_path / "new" / "smoke", "sample_mean")
    first.charge("save", Decimal(".1"), lambda: None)
    second.charge("save", Decimal(".1"), lambda: None)
    state = json.loads(budget.path.read_text())
    assert len(state["charges"]) == 2


def test_local_reservation_failure_never_marks_backward_submitted(
    tmp_path: Path, budget: CostStore
) -> None:
    batch = build_batch(((Turn((1, 2), (3,), (-0.1,)),),), (1.0,))
    client = Client()
    trainer = TrainerV2(client, budget, tmp_path, "sample_mean", Decimal("50"))
    with pytest.raises(ValueError, match="evaluation reserve"):
        trainer.update(0, batch)
    assert UpdateJournal.load(tmp_path / "update_000.json").state == "prepared"
    assert client.calls == 0
