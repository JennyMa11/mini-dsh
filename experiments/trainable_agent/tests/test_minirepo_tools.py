"""Real Docker contracts for controlled multi-file tools and atomic patch rejection."""

import pytest
from minidsh_rl.minirepo_tasks import development_tasks
from minidsh_rl.minirepo_tools import RepoSession
from minidsh_rl.minirepo_verification import IMAGE, seed

from minidsh.infrastructure.containers import ContainerResources, DockerHost, DockerSandbox
from minidsh.infrastructure.process import CommandPolicy
from minidsh.tools.runtime import ToolError


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
