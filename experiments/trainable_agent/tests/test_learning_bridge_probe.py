"""Real isolated Loop control validates data wiring, not model capability."""

import json
from pathlib import Path

import pytest
from minidsh_rl.learning_bridge_probe import probe
from minidsh_rl.learning_feedback import verify_feedback
from minidsh_rl.learning_rollout import PolicyBinding, load_rollout
from minidsh_rl.learning_tasks import learning_cases
from minidsh_rl.minirepo_verification import IMAGE

from minidsh.infrastructure.containers import DockerHost


@pytest.mark.integration
async def test_scripted_real_container_bridge_keeps_control_provenance(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    ledger.write_text('{"unchanged":true}')
    output = tmp_path / "probe"
    await probe(output, ledger)
    summary = json.loads((output / "summary.json").read_text())
    assert summary["passed"] is True
    assert summary["control_rollouts"] == 4
    assert summary["new_provider_calls"] == summary["new_training_calls"] == 0
    assert summary["scripted_trajectories_rejected_for_training"] is True
    assert summary["partial_test_exit_codes"] == [1, 0]
    assert summary["single_sampled_control_calls"] == 1
    assert summary["single_synthetic_stop"] is True
    assert summary["empty_control_calls"] == 0
    assert summary["budget_ledger_unchanged"] is True
    assert summary["hidden_checker_absent_from_requests"] is True
    assert ledger.read_text() == '{"unchanged":true}'
    case = next(c for c in learning_cases() if c.task.family == "table_join" and c.variant == 0)
    for mode, expected in [("interactive", True), ("single", False)]:
        source = output / mode
        binding = json.loads((source / "learning-binding/turn_000.json").read_text())
        policy = PolicyBinding.model_validate(binding["policy"])
        rollout = load_rollout(case, source, policy)
        target = tmp_path / (mode + "-feedback")
        assert await verify_feedback(rollout, source, target, DockerHost(), IMAGE) is expected
        proof = json.loads((target / "feedback.json").read_text())
        assert proof["source_kind"] == "scripted_control"
        assert proof["failed_private_patch_indices"] == ([1] if expected else [])
        assert await verify_feedback(rollout, source, target, DockerHost(), IMAGE) is expected
