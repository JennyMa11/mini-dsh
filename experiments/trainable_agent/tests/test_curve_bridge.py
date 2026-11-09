"""Real networkless container control, never a model learning result."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from minidsh_rl.curve_cases_v2 import curve_cases_v2
from minidsh_rl.learning_feedback import verify_feedback
from minidsh_rl.learning_rollout import PolicyBinding, assemble_group, run_learning_rollout
from minidsh_rl.minirepo_prompt_probe import ExampleBackend
from minidsh_rl.minirepo_verification import IMAGE

from minidsh.infrastructure.containers import DockerHost


@pytest.mark.integration
async def test_v2_profile_runs_real_feedback_without_private_context(tmp_path: Path) -> None:
    case = next(c for c in curve_cases_v2() if c.task.family == "table_join" and c.split == "dev")
    edits = [{"path": f.module, "old": f.new, "new": f.old} for f in case.faults]

    def patch(changes: list[dict[str, str]]) -> str:
        return (
            "<tool_call>"
            + json.dumps({"name": "apply_patch", "arguments": {"edits": changes}})
            + "</tool_call>"
        )

    test = '<tool_call>{"name":"run_tests","arguments":{}}</tool_call>'
    backend = ExampleBackend([patch(edits[:1]), test, patch(edits[1:]), test, "Done."])
    policy = PolicyBinding(
        kind="scripted_control",
        policy_id="curve_scripted_control",
        snapshot_sha256="a" * 64,
        tokenizer_sha256=hashlib.sha256(b"synthetic_not_provider_tokens").hexdigest(),
    )
    value = await run_learning_rollout(
        case,
        backend,
        policy=policy,
        run_id="curve_scripted",
        seed=17,
        output=tmp_path / "run",
        host=DockerHost(),
        image=IMAGE,
    )
    assert value.success
    assert len(value.turns) == 5
    result = json.loads((tmp_path / "run/result.json").read_text())
    assert result["candidate_policy"] == "curve_pure_v1"
    assert result["prompt_variant"] == "tool_example"
    assert await verify_feedback(
        value, tmp_path / "run", tmp_path / "feedback", DockerHost(), IMAGE
    )
    assert all(
        case.task.hidden_checks not in message.content
        for request in backend.requests
        for message in request.messages
    )
    with pytest.raises(ValueError, match="cross-case"):
        assemble_group(tuple(replace(value, run_id=f"control_{i}", seed=i) for i in range(4)))
