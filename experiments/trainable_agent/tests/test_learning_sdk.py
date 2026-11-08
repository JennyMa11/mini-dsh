"""Fake SDK calls prove ordering and identity contracts, never empirical training."""

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from minidsh_rl import learning_sdk
from minidsh_rl.backend_v2 import Tokenizer
from minidsh_rl.contracts import Turn
from minidsh_rl.cost_store import CostStore
from minidsh_rl.datum import build_batch
from minidsh_rl.learning_execution import Checkpoint, ExecutionState, GroupRecord, group_datum_hash
from minidsh_rl.learning_rollout import LearningGroup, LearningRollout
from minidsh_rl.learning_schedule import atomic_record, make_plan
from minidsh_rl.learning_sdk import LearningSDKRuntime, tokenizer_pin
from minidsh_rl.learning_tasks import learning_cases
from minidsh_rl.priced_backend import PricedBackend
from minidsh_rl.transaction import UpdateJournal

from minidsh.infrastructure.process import ProcessRunner


class FakeTokenizer:
    chat_template = "unit template"

    def get_vocab(self) -> dict[str, int]:
        return {"unit": 1}


@dataclass
class Saved:
    path: str
    type: str
    model: str = "Qwen/Qwen3.5-4B"
    size: int = 1

    def model_dump_json(self, *, indent: int | None = None) -> str:
        return json.dumps(vars(self), indent=indent)


@dataclass
class Future[T]:
    value: T
    fail: bool = False

    def result(self) -> T:
        if self.fail:
            raise TimeoutError("unknown optimizer")
        return self.value


@dataclass
class Tensor:
    data: tuple[float, ...] = (-0.1,)


@dataclass
class Forward:
    metrics: dict[str, float] = field(default_factory=lambda: {"loss_sum": 0.0})
    loss_fn_outputs: list[dict[str, Tensor]] = field(
        default_factory=lambda: [{"logprobs": Tensor()} for _ in range(4)]
    )

    def model_dump_json(self, *, indent: int) -> str:
        return json.dumps({"metrics": self.metrics}, indent=indent)


@dataclass
class Client:
    calls: list[str]
    training_run_cuid: str = "unit_training"
    model_id: str = "Qwen/Qwen3.5-4B"
    fail_optimizer: bool = False

    def get_tokenizer(self) -> FakeTokenizer:
        self.calls.append("tokenizer")
        return FakeTokenizer()

    def save_state(self, name: str) -> Future[Saved]:
        self.calls.append("save_state")
        return Future(Saved(f"trio://unit/weights/{name}", "save-checkpoint"))

    def save_weights_for_sampler(self, name: str) -> Future[Saved]:
        self.calls.append("save_sampler")
        return Future(Saved(f"trio://unit/sampler_weights/{name}", "save-weights"))

    def forward_backward(self, _datums: list[object], **_kwargs: object) -> Future[Forward]:
        self.calls.append("backward")
        return Future(Forward())

    def optim_step(self, _parameters: object) -> Future[Forward]:
        self.calls.append("optimizer")
        return Future(Forward(), self.fail_optimizer)


@dataclass
class Sampler:
    sampling_run_cuid: str = "unit_sampling"
    model_id: str = "Qwen/Qwen3.5-4B"


@dataclass
class Service:
    calls: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    fail_connect: bool = False
    fail_optimizer: bool = False

    def create_lora_training_client(self, **settings: object) -> Client:
        assert settings == {
            "base_model": "Qwen/Qwen3.5-4B",
            "rank": 16,
            "seed": 17,
            "train_mlp": True,
            "train_attn": True,
            "train_unembed": False,
        }
        self.calls.append("create_lora")
        return Client(self.calls, fail_optimizer=self.fail_optimizer)

    def create_training_client_from_state_with_optimizer(self, path: str) -> Client:
        self.calls.append("restore_optimizer")
        self.paths.append(path)
        return Client(self.calls)

    def create_sampling_client(self, *, base_model: str, model_path: str) -> Sampler:
        assert base_model == "Qwen/Qwen3.5-4B"
        self.calls.append("connect_sampler")
        self.paths.append(model_path)
        if self.fail_connect:
            raise TimeoutError("unknown sampler connection")
        return Sampler()


def store(tmp_path: Path) -> CostStore:
    old = tmp_path / "old.json"
    old.write_text('{"schema_version":1,"limit":"50","spent":"0","pending":{},"settled":[]}')
    quote = tmp_path / "quote.json"
    quote.write_text("{}")
    return CostStore(
        tmp_path / "budget.json",
        legacy=old,
        quote=quote,
        limit=Decimal("50"),
        uncertainty=Decimal("0"),
    )


def runtime(
    tmp_path: Path,
    budget: CostStore,
    service: Service,
    *,
    resume_allowance: int = 2,
) -> LearningSDKRuntime:
    root = tmp_path / "study"
    root.mkdir(exist_ok=True)
    return LearningSDKRuntime(
        service,
        budget,
        root,
        cast(ProcessRunner, object()),
        tokenizer_pin(cast(Tokenizer, FakeTokenizer())),
        resume_allowance=resume_allowance,
    )


async def test_initialize_and_restore_use_persistent_pair_and_same_tokenizer(
    tmp_path: Path,
) -> None:
    with store(tmp_path) as budget:
        service = Service()
        adapter = runtime(tmp_path, budget, service)
        assert service.calls == []
        checkpoint = await adapter.initialize(tmp_path / "study")
        assert service.calls == [
            "create_lora",
            "tokenizer",
            "save_state",
            "save_sampler",
            "connect_sampler",
        ]
        assert checkpoint.policy.kind == "provider"
        assert checkpoint.optimizer_uri != checkpoint.sampler_uri
        assert budget.committed == Decimal(".21")
        state = ExecutionState(
            plan_sha256="a" * 64,
            rows={},
            initial=checkpoint,
            current=checkpoint,
            resumes=1,
            pending_kind="resume",
        )
        atomic_record(tmp_path / "study/state.json", state)
        replacement_service = Service()
        replacement = runtime(tmp_path, budget, replacement_service)
        await replacement.restore(checkpoint, tmp_path / "study")
        assert replacement_service.calls == ["restore_optimizer", "tokenizer", "connect_sampler"]
        assert replacement_service.paths == [checkpoint.optimizer_uri, checkpoint.sampler_uri]
        assert budget.committed == Decimal(".22")


async def test_unknown_connect_does_not_publish_initial_receipt(tmp_path: Path) -> None:
    with store(tmp_path) as budget:
        service = Service(fail_connect=True)
        adapter = runtime(tmp_path, budget, service)
        with pytest.raises(TimeoutError, match="unknown"):
            await adapter.initialize(tmp_path / "study")
        assert not (tmp_path / "study/initial.json").exists()
        charges = json.loads(budget.path.read_text())["charges"]
        assert sum(v["status"] == "pending" for v in charges.values()) == 1
        assert service.calls.count("connect_sampler") == 1


async def test_staged_sdk_restore_after_two_resumes_requires_explicit_allowance(
    tmp_path: Path,
) -> None:
    with store(tmp_path) as budget:
        adapter = runtime(tmp_path, budget, Service())
        checkpoint = await adapter.initialize(tmp_path / "study")
        state = ExecutionState(
            plan_sha256="a" * 64,
            rows={},
            initial=checkpoint,
            current=checkpoint,
            resumes=3,
            pending_kind="resume",
        )
        atomic_record(tmp_path / "study/state.json", state)
        service = Service()
        default = runtime(tmp_path, budget, service)
        with pytest.raises(ValueError, match="admitted"):
            await default.restore(checkpoint, tmp_path / "study")
        assert service.calls == []
        staged = runtime(tmp_path, budget, service, resume_allowance=12)
        await staged.restore(checkpoint, tmp_path / "study")
        assert service.calls == ["restore_optimizer", "tokenizer", "connect_sampler"]
        assert budget.committed == Decimal(".22")


async def test_tokenizer_change_stops_before_saving_or_sampling(tmp_path: Path) -> None:
    with store(tmp_path) as budget:
        service = Service()
        adapter = runtime(tmp_path, budget, service)
        adapter.expected_tokenizer = "a" * 64
        with pytest.raises(ValueError, match="tokenizer"):
            await adapter.initialize(tmp_path / "study")
        assert service.calls == ["create_lora", "tokenizer"]


def group_record(directory: Path, before: Checkpoint, *, signal: bool = True) -> LearningGroup:
    advantages = (-0.5, 0.5, -0.5, 0.5) if signal else (0.0,) * 4
    group = LearningGroup(
        tuple(f"unit_{i}" for i in range(4)),
        (0.0, 1.0, 0.0, 1.0) if signal else (0.0,) * 4,
        advantages,
        build_batch(((Turn((1,), (2,), (-0.1,)),),) * 4, advantages),
    )
    directory.mkdir(parents=True)
    atomic_record(
        directory / "group.json",
        GroupRecord(
            before=before,
            run_ids=group.run_ids,
            datum_sha256=group_datum_hash(group),
            has_signal=signal,
            input_sha256={},
        ),
        create=True,
    )
    return group


async def test_effective_update_saves_both_states_before_publishing_receipt(tmp_path: Path) -> None:
    with store(tmp_path) as budget:
        service = Service()
        adapter = runtime(tmp_path, budget, service)
        before = await adapter.initialize(tmp_path / "study")
        folder = tmp_path / "study/updates/train_000"
        group = group_record(folder, before)
        await adapter.update("train_000", group, before, folder)
        assert service.calls[5:] == [
            "backward",
            "optimizer",
            "save_state",
            "save_sampler",
            "connect_sampler",
        ]
        receipt = json.loads((folder / "receipt.json").read_text())
        journal = UpdateJournal.load(folder / "update_000.json")
        assert receipt["updated"] is True
        assert journal.state == "checkpointed"
        assert journal.checkpoint == receipt["after"]["optimizer_uri"]
        assert before.policy.policy_id in adapter.samplers
        assert len(adapter.samplers) == 2


async def test_unknown_optimizer_does_not_save_or_replay(tmp_path: Path) -> None:
    with store(tmp_path) as budget:
        service = Service(fail_optimizer=True)
        adapter = runtime(tmp_path, budget, service)
        before = await adapter.initialize(tmp_path / "study")
        folder = tmp_path / "study/updates/train_000"
        group = group_record(folder, before)
        with pytest.raises(TimeoutError, match="optimizer"):
            await adapter.update("train_000", group, before, folder)
        assert UpdateJournal.load(folder / "update_000.json").state == "optim_submitted"
        assert not (folder / "receipt.json").exists()
        with pytest.raises(FileExistsError):
            await adapter.update("train_000", group, before, folder)
        assert service.calls.count("optimizer") == 1
        assert service.calls.count("save_state") == 1


async def test_zero_signal_publishes_skip_without_sdk_calls(tmp_path: Path) -> None:
    with store(tmp_path) as budget:
        service = Service()
        adapter = runtime(tmp_path, budget, service)
        before = await adapter.initialize(tmp_path / "study")
        folder = tmp_path / "study/updates/train_000"
        group = group_record(folder, before, signal=False)
        calls = list(service.calls)
        committed = budget.committed
        await adapter.update("train_000", group, before, folder)
        assert service.calls == calls
        assert budget.committed == committed
        assert adapter.current == before
        receipt = json.loads((folder / "receipt.json").read_text())
        assert receipt["updated"] is False
        assert receipt["before"] == receipt["after"]


async def test_restore_reconnects_initial_base_and_current_rl_separately(tmp_path: Path) -> None:
    with store(tmp_path) as budget:
        adapter = runtime(tmp_path, budget, Service())
        initial = await adapter.initialize(tmp_path / "study")
        folder = tmp_path / "study/updates/train_000"
        await adapter.update("train_000", group_record(folder, initial), initial, folder)
        current = adapter.current
        assert current is not None
        state = ExecutionState(
            plan_sha256="a" * 64,
            rows={},
            initial=initial,
            current=current,
            resumes=1,
            pending_kind="resume",
        )
        atomic_record(tmp_path / "study/state.json", state)
        service = Service()
        replacement = runtime(tmp_path, budget, service)
        await replacement.restore(current, tmp_path / "study")
        assert service.paths == [current.optimizer_uri, current.sampler_uri, initial.sampler_uri]
        assert len(replacement.samplers) == 2
        assert "create_lora" not in service.calls
        assert "save_state" not in service.calls


async def test_failed_sampler_save_after_optimizer_has_no_commit_receipt(tmp_path: Path) -> None:
    with store(tmp_path) as budget:
        service = Service()
        adapter = runtime(tmp_path, budget, service)
        before = await adapter.initialize(tmp_path / "study")
        folder = tmp_path / "study/updates/train_000"
        service.fail_connect = True
        with pytest.raises(TimeoutError, match="sampler"):
            await adapter.update("train_000", group_record(folder, before), before, folder)
        assert UpdateJournal.load(folder / "update_000.json").state == "optim_confirmed"
        assert not (folder / "receipt.json").exists()
        assert adapter.current == before


async def test_changed_policy_metadata_is_rejected_before_restore_sdk(tmp_path: Path) -> None:
    with store(tmp_path) as budget:
        adapter = runtime(tmp_path, budget, Service())
        before = await adapter.initialize(tmp_path / "study")
        path = tmp_path / "study/policies" / (before.policy.policy_id + ".json")
        value = json.loads(path.read_text())
        value["sampler"]["path"] = "trio://wrong/sampler"
        path.write_text(json.dumps(value))
        service = Service()
        replacement = runtime(tmp_path, budget, service)
        with pytest.raises(ValueError, match="binding"):
            await replacement.restore(before, tmp_path / "study")
        assert service.calls == []


async def test_tokenizer_mutation_blocks_rollout_before_backend(tmp_path: Path) -> None:
    with store(tmp_path) as budget:
        service = Service()
        adapter = runtime(tmp_path, budget, service)
        before = await adapter.initialize(tmp_path / "study")
        assert adapter.tokenizer is not None
        adapter.tokenizer.chat_template = "changed"
        row = make_plan("contract_fixture", experiment_id="unit").rows[0]
        case = next(c for c in learning_cases() if c.task.case_id == row.case_id)
        calls = list(service.calls)
        with pytest.raises(ValueError, match="policy/case"):
            await adapter.rollout(case, row, before, tmp_path / "unused")
        assert service.calls == calls
        assert not (tmp_path / "unused").exists()


async def test_rollout_wires_priced_backend_and_original_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with store(tmp_path) as budget:
        service = Service()
        adapter = runtime(tmp_path, budget, service)
        before = await adapter.initialize(tmp_path / "study")
        row = make_plan("contract_fixture", experiment_id="unit").rows[0]
        case = next(c for c in learning_cases() if c.task.case_id == row.case_id)
        expected = LearningRollout(
            case=case,
            run_id=row.run_id,
            seed=row.seed,
            policy=before.policy,
            success=False,
            status="failed",
            turns=(),
            total_tokens=0,
            harness_sha256="c" * 64,
            verification_executed=False,
            local_failed_calls=0,
        )

        async def fake(*args: object, **kwargs: object) -> LearningRollout:
            assert args[0] == case
            backend = args[1]
            assert isinstance(backend, PricedBackend)
            assert backend.budget is budget
            assert backend.policy_id == before.policy.policy_id
            assert kwargs["policy"] == before.policy
            assert kwargs["run_id"] == row.run_id
            assert kwargs["seed"] == row.seed
            return expected

        monkeypatch.setattr(learning_sdk, "run_learning_rollout", fake)
        calls = list(service.calls)
        result = await adapter.rollout(case, row, before, tmp_path / "unused")
        assert result == expected
        assert service.calls == calls


async def test_initial_check_is_recorded_before_initial_receipt(tmp_path: Path) -> None:
    with store(tmp_path) as budget:
        adapter = runtime(tmp_path, budget, Service())

        def check(
            _runtime: LearningSDKRuntime, _checkpoint: Checkpoint, directory: Path
        ) -> Decimal:
            assert not (directory.parent / "initial.json").exists()
            budget.reserve("unit_initial_audit", Decimal(".02"))
            budget.settle("unit_initial_audit", Decimal(".02"))
            return Decimal(".02")

        adapter.initial_check = check
        result = await adapter.initialize(tmp_path / "study")
        assert result.preparation_spent == Decimal(".02")
        assert budget.committed == Decimal(".23")


async def test_initial_check_failure_cannot_publish_ready_policy(tmp_path: Path) -> None:
    with store(tmp_path) as budget:
        adapter = runtime(tmp_path, budget, Service())

        def check(
            _runtime: LearningSDKRuntime, _checkpoint: Checkpoint, _directory: Path
        ) -> Decimal:
            raise ValueError("initial adapter audit failed")

        adapter.initial_check = check
        with pytest.raises(ValueError, match="audit failed"):
            await adapter.initialize(tmp_path / "study")
        assert not (tmp_path / "study/initial.json").exists()
