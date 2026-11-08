"""Unit artifacts are fabricated contract inputs, never empirical model evidence."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from minidsh_rl.contracts import Turn
from minidsh_rl.learning_rollout import (
    PolicyBinding,
    assemble_group,
    load_rollout,
    seal_rollout,
    turn_digest,
)
from minidsh_rl.learning_tasks import learning_cases
from minidsh_rl.minirepo_prompts import instruction


def policy(kind: str = "provider") -> PolicyBinding:
    return PolicyBinding.model_validate(
        {
            "kind": kind,
            "policy_id": "contract_fixture",
            "snapshot_sha256": "a" * 64,
            "tokenizer_sha256": "b" * 64,
        }
    )


def fixture(directory: Path, *, success: bool = True, seed: int = 17) -> None:
    """Small synthetic files for strict loader tests, not provider trace generation."""
    case = learning_cases()[0]
    directory.mkdir()
    turn = Turn((11, 12), (13, 14), (-0.1, -0.2))
    raw = {
        "schema_version": 1,
        "run_id": directory.name,
        "call_index": 0,
        "policy_id": "contract_fixture",
        "prompt_token_ids": turn.prompt,
        "completion_token_ids": turn.completion,
        "old_logprobs": turn.logprobs,
        "seed": seed,
        "temperature": 1.0,
        "top_p": 1.0,
        "raw_text": "<tool_call>broken",
        "stop_reason": "length",
    }
    (directory / "tokens").mkdir()
    (directory / "tokens/turn_000.json").write_text(json.dumps(raw))
    for phase in ("sample-started", "sample-returned"):
        (directory / phase).mkdir()
        receipt = {
            "schema_version": 1,
            "call_index": 0,
            "seed": seed,
            "prompt_sha256": turn_digest(turn.prompt),
        }
        if phase == "sample-returned":
            receipt["turn_sha256"] = turn_digest(turn)
        (directory / phase / "turn_000.json").write_text(json.dumps(receipt))
    candidate = dict(case.task.reference)
    (directory / "candidate.json").write_text(json.dumps(candidate))
    candidate_sha = turn_digest(candidate)
    (directory / "verification").mkdir()
    (directory / "verification/result.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case_id": case.task.case_id,
                "criterion_version": "minirepo_dev_v1",
                "passed": success,
                "exit_code": 0 if success else 1,
                "timed_out": False,
                "candidate_sha256": candidate_sha,
            }
        )
    )
    result = {
        "schema_version": 1,
        "run_id": directory.name,
        "case_id": case.task.case_id,
        "policy_id": "contract_fixture",
        "family": case.task.family,
        "mode": "interactive",
        "prompt_variant": "original",
        "read_variant": "batch_read_eight",
        "instruction_sha256": hashlib.sha256(
            instruction(case.task, "interactive", "original").encode()
        ).hexdigest(),
        "seed": seed,
        "split": "development",
        "criterion_version": "minirepo_dev_v1",
        "success": success,
        "status": "budget_exhausted",
        "sampled_calls": 1,
        "synthetic_stop": False,
        "total_tokens": 4,
        "generated_tokens": 2,
        "tool_calls": 0,
        "steps": 1,
    }
    (directory / "result.json").write_text(json.dumps(result))
    events = [
        {"name": "run_started", "step_id": 0, "data": {}},
        {"name": "model_call_started", "step_id": 1, "data": {}},
        {
            "name": "model_call_completed",
            "step_id": 1,
            "data": {"input_tokens": 2, "output_tokens": 2, "usage_known": True},
        },
        {
            "name": "run_finished",
            "step_id": 1,
            "data": {
                "status": "budget_exhausted",
                "duration_ms": 1.0,
                "steps": 1,
                "tokens": 4,
                "tool_calls": 0,
            },
        },
    ]
    (directory / "trace.jsonl").write_text(
        "\n".join(
            json.dumps(
                e
                | {
                    "schema_version": "1.0",
                    "run_id": directory.name,
                    "occurred_at": "2026-01-01T00:00:00Z",
                }
            )
            for e in events
        )
        + "\n"
    )


async def test_roundtrip_preserves_raw_tokens_invalid_action_mask_and_legacy_records(
    tmp_path: Path,
) -> None:
    case = learning_cases()[0]
    directory = tmp_path / "contract_run"
    fixture(directory)
    before = (directory / "result.json").read_bytes()
    await seal_rollout(case, directory, policy())
    binding = json.loads((directory / "learning-binding/turn_000.json").read_text())
    assert binding["schema_version"] == 2
    assert binding["case_metadata"]["split"] == "train"
    assert binding["case_metadata"]["dataset_version"] == case.dataset_version
    assert binding["case_metadata"]["criterion_version"] == case.criterion_version
    result = load_rollout(case, directory, policy())
    assert result.success is True
    assert result.status == "budget_exhausted"
    assert result.turns == (Turn((11, 12), (13, 14), (-0.1, -0.2)),)
    assert result.case == case
    assert (directory / "result.json").read_bytes() == before
    batch = assemble_group(
        tuple(replace(result, run_id=f"run_{i}", seed=17 + i, success=i % 2 == 0) for i in range(4))
    ).batch
    assert batch is not None
    assert batch.datums[0].loss_fn_inputs["target_tokens"].data == [12, 13, 14]
    assert batch.datums[0].loss_fn_inputs["advantages"].data == [0.0, 0.5, 0.5]


@pytest.mark.parametrize(
    "fault", ["policy", "seed", "missing_raw", "unknown_sample", "verdict", "usage"]
)
async def test_inconsistent_or_unknown_inputs_cannot_be_sealed(tmp_path: Path, fault: str) -> None:
    case = learning_cases()[0]
    directory = tmp_path / "broken"
    fixture(directory)
    if fault in {"policy", "seed"}:
        p = directory / "tokens/turn_000.json"
        data = json.loads(p.read_text())
        data["policy_id" if fault == "policy" else "seed"] = "wrong" if fault == "policy" else 99
        p.write_text(json.dumps(data))
    elif fault == "missing_raw":
        (directory / "tokens/turn_000.json").unlink()
    elif fault == "unknown_sample":
        (directory / "sample-returned/turn_000.json").unlink()
    elif fault == "verdict":
        p = directory / "verification/result.json"
        data = json.loads(p.read_text())
        data["passed"] = False
        p.write_text(json.dumps(data))
    else:
        p = directory / "result.json"
        data = json.loads(p.read_text())
        data["total_tokens"] = 5
        p.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=r"binding|unknown|incomplete|usage|reward"):
        await seal_rollout(case, directory, policy())


async def test_sealed_artifact_tampering_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "sealed"
    fixture(directory)
    case = learning_cases()[0]
    await seal_rollout(case, directory, policy())
    with (directory / "tokens/turn_000.json").open("a") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="artifact"):
        load_rollout(case, directory, policy())


async def test_group_rejects_scripted_cross_case_cross_policy_duplicate_and_test_split(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "group"
    fixture(directory)
    case = learning_cases()[0]
    await seal_rollout(case, directory, policy())
    result = load_rollout(case, directory, policy())
    group = tuple(replace(result, run_id=f"g_{i}", seed=17 + i) for i in range(4))
    variants = [
        group[:3],
        (group[0],) * 4,
        tuple(replace(x, policy=policy("scripted_control")) for x in group),
        (*group[:3], replace(group[3], policy=policy("scripted_control"))),
        (
            *group[:3],
            replace(group[3], policy=policy().model_copy(update={"snapshot_sha256": "d" * 64})),
        ),
        (*group[:3], replace(group[3], case=learning_cases()[2])),
        tuple(
            replace(x, case=next(c for c in learning_cases() if c.split == "test")) for x in group
        ),
    ]
    for invalid in variants:
        with pytest.raises(ValueError, match=r"four|duplicate|training group"):
            assemble_group(invalid)
    empty = tuple(replace(x, success=False, turns=(), total_tokens=0) for x in group)
    assembled = assemble_group(empty)
    assert assembled.batch is None
    assert assembled.rewards == (0.0,) * 4
    assert assembled.advantages == (0.0,) * 4


async def test_local_pre_backend_failure_keeps_failed_trace_and_existing_actions(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "local_failure"
    fixture(directory, success=False)
    result = json.loads((directory / "result.json").read_text())
    result["status"] = "failed"
    result["steps"] = 2
    (directory / "result.json").write_text(json.dumps(result))
    trace = [json.loads(line) for line in (directory / "trace.jsonl").read_text().splitlines()]
    final = trace.pop()
    final["step_id"] = 2
    final["data"]["status"] = "failed"
    final["data"]["steps"] = 2
    base = {
        "schema_version": "1.0",
        "run_id": directory.name,
        "step_id": 2,
        "occurred_at": "2026-01-01T00:00:00Z",
    }
    trace.extend(
        [
            base | {"name": "model_call_started", "data": {}},
            base | {"name": "model_call_failed", "data": {"usage_known": False}},
            final,
        ]
    )
    (directory / "trace.jsonl").write_text("\n".join(map(json.dumps, trace)) + "\n")
    case = learning_cases()[0]
    await seal_rollout(case, directory, policy())
    loaded = load_rollout(case, directory, policy())
    assert loaded.local_failed_calls == 1
    assert loaded.success is False
    assert len(loaded.turns) == 1
    assert loaded.total_tokens == 4
    assert (
        json.loads((directory / "trace.jsonl").read_text().splitlines()[-2])["data"]["usage_known"]
        is False
    )


async def test_snapshot_version_and_tokenizer_are_not_interchangeable(tmp_path: Path) -> None:
    directory = tmp_path / "versions"
    fixture(directory)
    case = learning_cases()[0]
    await seal_rollout(case, directory, policy())
    with pytest.raises(ValueError, match="binding"):
        load_rollout(replace(case, criterion_version="other"), directory, policy())
    with pytest.raises(ValueError, match="binding"):
        load_rollout(case, directory, policy().model_copy(update={"tokenizer_sha256": "e" * 64}))


async def test_known_overrun_preserves_success_and_usage_but_blocks_training(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "overrun"
    fixture(directory)
    case = learning_cases()[0]
    turn = Turn((11,) * 6144, (13, 14), (-0.1, -0.2))
    p = directory / "tokens/turn_000.json"
    raw = json.loads(p.read_text())
    raw["prompt_token_ids"] = turn.prompt
    p.write_text(json.dumps(raw))
    for phase in ("sample-started", "sample-returned"):
        p = directory / phase / "turn_000.json"
        d = json.loads(p.read_text())
        d["prompt_sha256"] = turn_digest(turn.prompt)
        if phase == "sample-returned":
            d["turn_sha256"] = turn_digest(turn)
        p.write_text(json.dumps(d))
    p = directory / "result.json"
    d = json.loads(p.read_text())
    d["total_tokens"] = 6146
    p.write_text(json.dumps(d))
    p = directory / "trace.jsonl"
    events = [json.loads(x) for x in p.read_text().splitlines()]
    events[-2]["data"]["input_tokens"] = 6144
    events[-1]["data"]["tokens"] = 6146
    p.write_text("\n".join(map(json.dumps, events)) + "\n")
    await seal_rollout(case, directory, policy())
    loaded = load_rollout(case, directory, policy())
    assert loaded.success is True
    assert loaded.total_tokens == 6146
    assert "request_tokens" in loaded.budget_violations
    with pytest.raises(ValueError, match="budget"):
        assemble_group(tuple(replace(loaded, run_id=f"overrun_{i}", seed=17 + i) for i in range(4)))
