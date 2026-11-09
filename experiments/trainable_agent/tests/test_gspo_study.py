"""Same-size GSPO efficacy queue, separate from engineering Smoke and old results."""

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
import torch
from minidsh_rl import gspo_study_run
from minidsh_rl.contracts import Turn
from minidsh_rl.curve_execution import CurveState
from minidsh_rl.evaluate_v2 import evaluation_queue
from minidsh_rl.gspo import GSPOBatch, GSPOConfig, build_gspo_batch, make_loss
from minidsh_rl.gspo_study import CONFIG, StudyRow, batch_digest, execute_study, make_study
from minidsh_rl.gspo_study_analysis import audit_loss
from minidsh_rl.learning_execution import Checkpoint, UpdateReceipt
from minidsh_rl.learning_rollout import PolicyBinding
from minidsh_rl.learning_schedule import atomic_record, digest
from minidsh_rl.rollout_v2 import Rollout
from minidsh_rl.train_v2 import training_schedule
from minidsh_rl.transaction import UpdateJournal


def test_frozen_queue_matches_old_grpo_scale_and_order() -> None:
    plan = make_study()
    assert len(plan.rows) == 352
    assert Counter(r.phase for r in plan.rows) == {"train": 160, "validation": 64, "test": 128}
    for slot, task in enumerate(training_schedule("pilot")):
        rows = [r for r in plan.rows if r.phase == "train" and r.slot == slot]
        assert len(rows) == 4
        assert {r.case_id for r in rows} == {task.case_id}
        assert [r.seed for r in rows] == [17 + slot * 100 + i for i in range(4)]
    assert plan.test_previously_opened
    assert plan.comparison == "base_vs_gspo_only"
    assert make_study() == plan


def test_evaluation_is_paired_and_independent_of_training_rewards() -> None:
    plan = make_study()
    for phase in ("validation", "test"):
        rows = [r for r in plan.rows if r.phase == phase]
        old = evaluation_queue(phase)
        assert [(r.case_id, r.seed, r.policy) for r in rows] == [
            (r["case_id"], r["seed"], "gspo" if r["policy"] == "rl" else "base") for r in old
        ]
        for first, second in zip(rows[::2], rows[1::2], strict=True):
            assert first.case_id == second.case_id
            assert first.seed == second.seed
            assert {first.policy, second.policy} == {"base", "gspo"}


class Resources:
    available = Decimal("100")
    committed = Decimal("0")
    has_pending = False


class Runtime:
    execution_kind: Literal["provider", "contract_fixture"] = "contract_fixture"

    def __init__(self, *, zero: bool = False, unknown: bool = False) -> None:
        self.updates = 0
        self.samples = 0
        self.zero = zero
        self.unknown = unknown
        self.calls = 0

    def checkpoint(self) -> Checkpoint:
        return Checkpoint(
            execution_kind="contract_fixture",
            sampler_uri=f"contract://sampler/{self.updates}",
            optimizer_uri=f"contract://optimizer/{self.updates}",
            policy=PolicyBinding(
                kind="scripted_control",
                policy_id=f"control_{self.updates}",
                snapshot_sha256="a" * 64,
                tokenizer_sha256="b" * 64,
            ),
        )

    async def initialize(self, root: Path) -> Checkpoint:
        self.calls += 1
        checkpoint = self.checkpoint()
        atomic_record(root / "initial.json", checkpoint, create=True)
        return checkpoint

    async def reconnect(self, before: Checkpoint, attempt: int) -> None:
        assert before == self.checkpoint()
        assert attempt <= 2
        self.calls += 1

    async def sample(
        self, row: StudyRow, before: Checkpoint, output: Path, reserve: Decimal
    ) -> Rollout:
        self.calls += 1
        self.samples += 1
        assert reserve > 0
        success = not self.zero and row.phase == "train" and row.run_id.endswith(("_0", "_1"))
        output.mkdir(parents=True)  # noqa: ASYNC240 - local fixture
        (output / "result.json").write_text(
            json.dumps(
                {
                    "run_id": row.run_id,
                    "case_id": row.case_id,
                    "seed": row.seed,
                    "policy_id": before.policy.policy_id,
                    "criterion_version": "expansion_v2",
                    "success": success,
                    "status": "finished",
                }
            )
        )
        return Rollout(
            row.run_id, success, "finished", (Turn((1,), (2,), (-0.2,)),), 1, 2, 1.0, "a" * 64, None
        )

    async def update(
        self, key: str, batch: GSPOBatch, before: Checkpoint, output: Path, reserve: Decimal
    ) -> None:
        assert reserve > 0
        self.calls += 1
        slot = int(key.rsplit("_", 1)[-1])
        journal_path = output / f"update_{slot:03}.json"
        journal = UpdateJournal.create(journal_path, batch_digest(batch))
        if batch.has_signal:
            for phase in ("backward_submitted", "backward_confirmed", "optim_submitted"):
                journal.advance(phase)
            if self.unknown:
                raise RuntimeError("unknown optimizer request")
            journal.advance("optim_confirmed")
            self.updates += 1
            after = self.checkpoint()
            journal.advance("checkpointed", checkpoint=after.optimizer_uri)
        else:
            journal.advance("skipped")
            after = before
        atomic_record(
            output / "receipt.json",
            UpdateReceipt(
                group_sha256=digest(output / "group.json"),
                before=before,
                after=after,
                updated=batch.has_signal,
                journal_sha256=digest(journal_path),
            ),
            create=True,
        )


async def test_complete_40_groups_and_all_evaluation_even_with_zero_validation(
    tmp_path: Path,
) -> None:
    plan = make_study()
    atomic_record(tmp_path / "plan.json", plan, create=True)
    runtime = Runtime()
    state = await execute_study(tmp_path, plan, Resources(), runtime)
    assert state.phase == "finished"
    assert len(state.completed) == 352
    assert len(state.groups) == 40
    assert runtime.updates == 40
    calls = runtime.calls
    assert await execute_study(tmp_path, plan, Resources(), runtime) == state
    assert runtime.calls == calls


async def test_zero_signal_skips_optimizer_and_retains_entire_declared_queue(
    tmp_path: Path,
) -> None:
    plan = make_study()
    atomic_record(tmp_path / "plan.json", plan, create=True)
    runtime = Runtime(zero=True)
    state = await execute_study(tmp_path, plan, Resources(), runtime)
    assert state.phase == "zero_signal_stop"
    assert runtime.updates == 0
    assert len(state.completed) == 40
    assert len(plan.rows) == 352


async def test_unknown_optimizer_is_never_replayed(tmp_path: Path) -> None:
    plan = make_study()
    atomic_record(tmp_path / "plan.json", plan, create=True)
    runtime = Runtime(unknown=True)
    with pytest.raises(RuntimeError, match="unknown optimizer"):
        await execute_study(tmp_path, plan, Resources(), runtime)
    calls = runtime.calls
    with pytest.raises(FileNotFoundError):
        await execute_study(tmp_path, plan, Resources(), runtime, resume=True)
    assert runtime.calls == calls
    state = CurveState.model_validate_json((tmp_path / "state.json").read_bytes())
    assert state.pending == "update"


async def test_insufficient_stage_admission_makes_no_calls(tmp_path: Path) -> None:
    plan = make_study()
    atomic_record(tmp_path / "plan.json", plan, create=True)
    resources = Resources()
    resources.available = Decimal("1")
    runtime = Runtime()
    with pytest.raises(ValueError, match="not funded"):
        await execute_study(tmp_path, plan, resources, runtime)
    assert runtime.calls == 0


def test_raw_gspo_loss_and_prompt_mask_audit(tmp_path: Path) -> None:
    batch = build_gspo_batch(
        ((Turn((1, 2), (3,), (-0.2,)),), (Turn((1, 2), (4,), (-0.3,)),)), (0.5, -0.5)
    )
    values = [[0.0, -0.2], [0.0, -0.3]]
    tensors = [torch.tensor(v, requires_grad=True) for v in values]
    _, metrics = make_loss(batch, CONFIG)(list(batch.datums), tensors)
    _, paper = make_loss(batch, GSPOConfig())(list(batch.datums), tensors)
    metrics["gspo/paper_clip_fraction"] = paper["gspo/active_clip_fraction"]
    (tmp_path / "current_logprobs_000.json").write_text(json.dumps(values))
    metrics_path = tmp_path / "custom_metrics_000.json"
    metrics_path.write_text(json.dumps(metrics))
    audit_loss(tmp_path, 0, batch)
    metrics["gspo/remote_scale"] = 2.0
    metrics_path.write_text(json.dumps(metrics))
    with pytest.raises(ValueError, match="metrics differ"):
        audit_loss(tmp_path, 0, batch)


async def test_changed_completed_evidence_blocks_before_new_calls(tmp_path: Path) -> None:
    plan = make_study()
    atomic_record(tmp_path / "plan.json", plan, create=True)
    runtime = Runtime(zero=True)
    state = await execute_study(tmp_path, plan, Resources(), runtime)
    (tmp_path / "rollouts" / state.completed[0] / "result.json").write_text("{}")
    calls = runtime.calls
    with pytest.raises(ValueError, match="confirmed evidence changed"):
        await execute_study(tmp_path, plan, Resources(), runtime, resume=True)
    assert runtime.calls == calls


async def test_provider_factory_not_created_before_stage_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = make_study()
    atomic_record(tmp_path / "plan.json", plan, create=True)
    resources = Resources()
    resources.available = Decimal("1")

    class Budget:
        def __enter__(self) -> Resources:
            return resources

        def __exit__(self, *args: object) -> None:
            pass

    def forbidden() -> None:
        raise AssertionError("SDK factory reached before admission")

    monkeypatch.setattr(gspo_study_run, "load_study", lambda _root: plan)
    monkeypatch.setattr(gspo_study_run, "CurveBudget", lambda _root: Budget())
    monkeypatch.setattr(gspo_study_run, "FencedResources", lambda _budget, _proof: resources)
    monkeypatch.setattr("minidsh_rl.gspo_study_run.trio.ServiceClient", forbidden)
    with pytest.raises(ValueError, match="stage and all evaluation unavailable"):
        await gspo_study_run.run(tmp_path)
