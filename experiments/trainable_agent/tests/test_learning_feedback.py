"""Trace/operation agreement and a new sampled response are required for recovery."""

import json
from pathlib import Path
from typing import cast

import pytest
from minidsh_rl import learning_feedback
from minidsh_rl.learning_feedback import feedback_indices
from minidsh_rl.learning_rollout import LearningRollout, PolicyBinding
from minidsh_rl.learning_tasks import learning_cases
from minidsh_rl.verification import Verdict

from minidsh.infrastructure.process import ProcessRunner


def fixture(directory: Path, *, sampled_after_failure: bool) -> None:
    operations = [
        {"operation": "patch", "patches": 1, "exit_code": None},
        {"operation": "test", "patches": 1, "exit_code": 1},
        {"operation": "patch", "patches": 2, "exit_code": None},
    ]
    records = []
    for i, tool in enumerate(["apply_patch", "run_tests", "apply_patch"]):
        if i < 2 or sampled_after_failure:
            records.append(
                {
                    "name": "model_call_completed",
                    "run_id": "unit",
                    "step_id": i + 1,
                    "data": {"output_tokens": 1},
                }
            )
        records.append(
            {
                "name": "tool_call_completed",
                "run_id": "unit",
                "step_id": i + 1,
                "data": {
                    "tool": tool,
                    "status": "succeeded",
                    "result": {"exit_code": 1} if i == 1 else {},
                },
            }
        )
    (directory / "operations.json").write_text(json.dumps(operations))
    (directory / "trace.jsonl").write_text("\n".join(json.dumps(r) for r in records))


@pytest.mark.parametrize("sampled_after_failure", [True, False])
def test_feedback_requires_subsequent_sample(
    tmp_path: Path, *, sampled_after_failure: bool
) -> None:
    fixture(tmp_path, sampled_after_failure=sampled_after_failure)
    assert feedback_indices(tmp_path, "unit") == ((1,) if sampled_after_failure else ())


def test_feedback_operation_tamper_is_rejected(tmp_path: Path) -> None:
    fixture(tmp_path, sampled_after_failure=True)
    path = tmp_path / "operations.json"
    operations = json.loads(path.read_text())
    operations[1]["exit_code"] = 0
    path.write_text(json.dumps(operations))
    with pytest.raises(ValueError, match="operations/trace"):
        feedback_indices(tmp_path, "unit")


@pytest.mark.parametrize(
    ("exit_code", "timed_out", "expected"),
    [
        (1, False, True),
        (-1, False, False),
        (0, False, False),
        (1, True, False),
    ],
)
async def test_only_executed_functional_failure_counts_as_pre_revision_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    *,
    timed_out: bool,
    expected: bool,
) -> None:
    case = learning_cases()[0]
    rollout = LearningRollout(
        case=case,
        run_id="unit",
        seed=17,
        policy=PolicyBinding(
            kind="scripted_control",
            policy_id="unit",
            snapshot_sha256="a" * 64,
            tokenizer_sha256="b" * 64,
        ),
        success=True,
        status="finished",
        turns=(),
        total_tokens=0,
        harness_sha256="c" * 64,
        verification_executed=True,
        local_failed_calls=0,
    )
    source = tmp_path / "source"
    source.mkdir()
    for name in [
        "learning-binding/turn_000.json",
        "trace.jsonl",
        "operations.json",
        "candidate.json",
        "verification/result.json",
    ]:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    (source / "patch-snapshots.json").write_text(json.dumps([dict(case.task.reference)]))
    monkeypatch.setattr(learning_feedback, "load_rollout", lambda *_args: rollout)
    monkeypatch.setattr(learning_feedback, "feedback_indices", lambda *_args: (1,))

    calls = []

    async def checker(*_args: object) -> Verdict:
        calls.append(True)
        target = cast(Path, _args[-1])
        target.mkdir(parents=True)
        (target / "result.json").write_text("{}")
        return Verdict(passed=False, exit_code=exit_code, timed_out=timed_out, duration_ms=0)

    monkeypatch.setattr(learning_feedback, "verify", checker)
    result = await learning_feedback.verify_feedback(
        rollout,
        source,
        tmp_path / "analysis",
        cast(ProcessRunner, object()),
        "unit-image",
    )
    assert result is expected
    assert (
        await learning_feedback.verify_feedback(
            rollout, source, tmp_path / "analysis", cast(ProcessRunner, object()), "unit-image"
        )
        is expected
    )
    assert len(calls) == 1
    (tmp_path / "analysis/patch_001/result.json").write_text('{"changed":true}')
    with pytest.raises(ValueError, match="proof changed"):
        await learning_feedback.verify_feedback(
            rollout, source, tmp_path / "analysis", cast(ProcessRunner, object()), "unit-image"
        )
    assert len(calls) == 1
