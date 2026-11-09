"""Real Docker contracts for controlled multi-file tools and atomic patch rejection."""

import json

import pytest
from minidsh_rl.curve_cases_v4 import curve_cases_v4
from minidsh_rl.minirepo_tasks import development_tasks
from minidsh_rl.minirepo_tools import RepoSession
from minidsh_rl.minirepo_verification import IMAGE, seed

from minidsh.infrastructure.containers import ContainerResources, DockerHost, DockerSandbox
from minidsh.infrastructure.process import CommandPolicy, ProcessResult
from minidsh.tools.runtime import ToolError


class _CaptureRunner:
    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str = ".",
        timeout_seconds: float = 30.0,
    ) -> ProcessResult:
        del cwd, timeout_seconds
        payload = json.loads(argv[3])
        result = {"files": self._files} if payload["operation"] == "capture" else {"exit_code": 0}
        return ProcessResult(argv, 0, json.dumps({"ok": True, "result": result}), "")


async def test_run_tests_uses_the_configured_candidate_policy() -> None:
    case = next(c for c in curve_cases_v4() if c.task.family == "dependency_batches")
    candidate = dict(case.task.reference)
    candidate["ready.py"] = candidate["ready.py"].replace(
        "graph[node] <= done", "graph[node].issubset(done)"
    )
    result = await RepoSession(
        _CaptureRunner(candidate), case.task, candidate_policy="curve_pure_v2"
    ).call("test", {})
    assert result == {"exit_code": 0}
    with pytest.raises(ToolError, match="candidate_policy_rejected"):
        await RepoSession(_CaptureRunner(candidate), case.task, candidate_policy="legacy").call(
            "test", {}
        )


@pytest.mark.integration
async def test_patch_rejection_is_atomic_and_submission_limit_enforced() -> None:
    task = development_tasks()[0]
    async with DockerSandbox(
        DockerHost(),
        image=IMAGE,
        resources=ContainerResources(
            platform="linux/arm64",
            cpus=1,
            memory_mb=256,
        ),
    ) as box:
        await seed(box, dict(task.files))
        session = RepoSession(box.runner("/app", CommandPolicy(frozenset({"python3"}))), task, 1)
        before = await session.call("read", {"path": "orders.py"})
        with pytest.raises(ToolError, match="patch_mismatch"):
            await session.call(
                "patch",
                {
                    "edits": [
                        {
                            "path": "orders.py",
                            "old": "totals[sku] = quantity",
                            "new": "totals[sku] = totals.get(sku, 0) + quantity",
                        },
                        {"path": "reservation.py", "old": "DOES_NOT_EXIST", "new": "x"},
                    ]
                },
            )
        assert await session.call("read", {"path": "orders.py"}) == before
        await session.call(
            "patch",
            {
                "edits": [
                    {
                        "path": "orders.py",
                        "old": "totals[sku] = quantity",
                        "new": "totals[sku] = totals.get(sku, 0) + quantity",
                    },
                    {
                        "path": "reservation.py",
                        "old": "remaining = stock",
                        "new": "remaining = dict(stock)",
                    },
                ]
            },
        )
        assert session.patches == 1
        with pytest.raises(ToolError, match="submission_limit"):
            await session.call("patch", {"edits": []})
        with pytest.raises(ToolError, match="path_denied"):
            await session.call("read", {"path": "../secret"})
        with pytest.raises(ToolError, match="path_denied"):
            await session.call(
                "patch", {"edits": [{"path": "public_tests.py", "old": "x", "new": "y"}]}
            )
        result = await session.call("test", {})
        assert isinstance(result, dict)
        assert result["exit_code"] == 0
