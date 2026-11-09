"""Actual tool execution of v4 profile; scripted tokens never become training data."""

import json
from pathlib import Path

import pytest
from minidsh_rl.curve_cases_v4 import curve_cases_v4
from minidsh_rl.learning_rollout import PolicyBinding, run_learning_rollout
from minidsh_rl.minirepo_prompt_probe import ExampleBackend
from minidsh_rl.minirepo_verification import IMAGE

from minidsh.infrastructure.containers import DockerHost


@pytest.mark.integration
async def test_v4_control_uses_frozen_feedback_and_private_verdict(tmp_path: Path) -> None:
    case = next(c for c in curve_cases_v4() if c.split == "train" and c.task.family == "table_join")
    edits = [{"path": f.module, "old": f.new, "new": f.old} for f in case.faults]
    action = (
        "<tool_call>"
        + json.dumps({"name": "apply_patch", "arguments": {"edits": edits}})
        + "</tool_call>"
    )
    backend = ExampleBackend(
        [action, '<tool_call>{"name":"run_tests","arguments":{}}</tool_call>', "Done."]
    )
    policy = PolicyBinding(
        kind="scripted_control",
        policy_id="v4_control",
        snapshot_sha256="a" * 64,
        tokenizer_sha256="b" * 64,
    )
    value = await run_learning_rollout(
        case,
        backend,
        policy=policy,
        run_id="v4_control",
        seed=17,
        output=tmp_path / "run",
        host=DockerHost(),
        image=IMAGE,
    )
    assert value.success
    assert value.case.dataset_version == "curve_dataset_v4"
    assert value.case.public_feedback_version == "curve_public_feedback_v3"
    raw = json.loads((tmp_path / "run/result.json").read_text())
    assert raw["prompt_variant"] == "tool_example"
    assert raw["candidate_policy"] == "curve_pure_v1"
    assert all(
        case.task.hidden_checks not in message.content
        for request in backend.requests
        for message in request.messages
    )
