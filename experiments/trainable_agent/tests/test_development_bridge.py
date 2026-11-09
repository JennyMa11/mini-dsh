"""Real sandbox control for detailed feedback, not a provider learning result."""

import json
from pathlib import Path

import pytest
from minidsh_rl.development_cases import development_cases
from minidsh_rl.learning_feedback import verify_feedback
from minidsh_rl.learning_rollout import PolicyBinding, run_learning_rollout
from minidsh_rl.minirepo_prompt_probe import ExampleBackend
from minidsh_rl.minirepo_verification import IMAGE

from minidsh.infrastructure.containers import DockerHost


@pytest.mark.integration
async def test_actionable_feedback_and_new_profile(tmp_path: Path) -> None:
    case = development_cases()[0]
    edits = [{"path": f.module, "old": f.new, "new": f.old} for f in case.faults]

    def patch(change: dict[str, str]) -> str:
        return (
            "<tool_call>"
            + json.dumps({"name": "apply_patch", "arguments": {"edits": [change]}})
            + "</tool_call>"
        )

    test = '<tool_call>{"name":"run_tests","arguments":{}}</tool_call>'
    backend = ExampleBackend([patch(edits[0]), test, patch(edits[1]), test, "Done."])
    policy = PolicyBinding(
        kind="scripted_control",
        policy_id="development_scripted",
        snapshot_sha256="a" * 64,
        tokenizer_sha256="b" * 64,
    )
    value = await run_learning_rollout(
        case,
        backend,
        policy=policy,
        run_id="scripted_v3",
        seed=17,
        output=tmp_path / "run",
        host=DockerHost(),
        image=IMAGE,
    )
    assert value.success
    assert await verify_feedback(
        value, tmp_path / "run", tmp_path / "feedback", DockerHost(), IMAGE
    )
    trace = [json.loads(line) for line in (tmp_path / "run/trace.jsonl").read_text().splitlines()]
    tests = [
        e["data"]["result"]
        for e in trace
        if e["name"] == "tool_call_completed" and e["data"].get("tool") == "run_tests"
    ]
    assert [t["exit_code"] for t in tests] == [1, 0]
    assert "longest_prefix" in tests[0]["stdout"]
    assert "expected=" in tests[0]["stdout"]
    assert all(
        case.task.hidden_checks not in m.content
        for request in backend.requests
        for m in request.messages
    )
