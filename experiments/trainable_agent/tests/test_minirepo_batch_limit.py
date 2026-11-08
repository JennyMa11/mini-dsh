"""The wider path list must preserve output, permission and historical contracts."""

import json
from pathlib import Path

import pytest
from minidsh_rl import minirepo_experiment as experiment
from minidsh_rl.minirepo_experiment import limit_evaluation_queue
from minidsh_rl.minirepo_tasks import development_tasks
from minidsh_rl.minirepo_tools import BatchRead, BatchReadEight, RepoSession, RepoTool
from minidsh_rl.minirepo_verification import IMAGE, seed
from pydantic import ValidationError

from minidsh.infrastructure.containers import ContainerResources, DockerHost, DockerSandbox
from minidsh.infrastructure.process import CommandPolicy
from minidsh.tools.runtime import ToolError


async def test_limit_study_rejects_control_for_a_different_path_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("mismatched controls must not connect to any service")

    monkeypatch.setattr("minidsh_rl.minirepo_experiment.trio.ServiceClient", forbidden)
    monkeypatch.setattr("minidsh_rl.minirepo_experiment.MetricRecorder.connect", forbidden)
    gates = tmp_path / "gates.json"
    gates.write_text(
        json.dumps({"passed": True, "completed": 56, "image": IMAGE, "source_sha256": {}})
    )
    control = tmp_path / "control.json"
    control.write_text(
        json.dumps(
            {
                "passed": True,
                "new_model_calls": 0,
                "same_observed_files": True,
                "source_sha256": {},
                "path_limits": {"batch_read": 4, "batch_read_eight": 4},
            }
        )
    )
    with pytest.raises(ValueError, match="batch-limit control"):
        await experiment.evaluate(
            tmp_path / "out",
            gates,
            tmp_path / "quote.json",
            mode="online",
            max_completion_per_call=1024,
            limit_study=True,
            batch_control=control,
        )
    assert not (tmp_path / "out").exists()


def test_schema_only_changes_maximum_items() -> None:
    narrow = BatchRead.model_json_schema()
    wide = BatchReadEight.model_json_schema()
    assert narrow["properties"]["paths"]["maxItems"] == 4
    assert wide["properties"]["paths"]["maxItems"] == 8
    narrow["properties"]["paths"]["maxItems"] = 8
    assert narrow == wide
    paths = list(dict(development_tasks()[0].files))
    assert len(paths) == 8
    assert BatchReadEight.model_validate({"paths": paths}).paths == paths
    with pytest.raises(ValidationError):
        BatchRead.model_validate({"paths": paths})
    for bad in [[], [*paths, "extra.py"], [paths[0], paths[0]], [1]]:
        with pytest.raises(ValidationError):
            BatchReadEight.model_validate({"paths": bad})
    with pytest.raises(ValidationError):
        BatchReadEight.model_validate({"paths": paths, "batch_path_limit": 16})


def test_limit_queue_only_changes_batch_path_cap() -> None:
    rows = limit_evaluation_queue()
    assert len(rows) == 32
    for i in range(0, len(rows), 2):
        a, b = rows[i : i + 2]
        assert {a["read_variant"], b["read_variant"]} == {"batch_read", "batch_read_eight"}
        assert (a["case_id"], a["seed"], a["repeat"]) == (b["case_id"], b["seed"], b["repeat"])
        assert a["mode"] == b["mode"] == "interactive"
        assert a["prompt_variant"] == b["prompt_variant"] == "original"


@pytest.mark.integration
async def test_wide_batch_keeps_shared_cap_paths_and_workspace() -> None:
    task = development_tasks()[0]
    async with DockerSandbox(
        DockerHost(),
        image=IMAGE,
        resources=ContainerResources(platform="linux/arm64", cpus=1, memory_mb=256),
    ) as box:
        await seed(box, dict(task.files))
        runner = box.runner("/app", CommandPolicy(frozenset({"python3"})))
        narrow = RepoSession(runner, task)
        wide = RepoSession(runner, task, batch_path_limit=8)
        before = await wide.capture()
        paths = sorted(dict(task.files))
        with pytest.raises(ToolError, match="invalid_arguments"):
            await narrow.call("read_many", {"paths": [*paths]})
        result = await wide.call("read_many", {"paths": [*paths]})
        assert isinstance(result, dict)
        assert result["returned_characters"] == 5000
        assert result["truncated"] is True
        assert await wide.capture() == before
        assert wide.patches == narrow.patches == 0
        tool = RepoTool(wide, "read_files")
        assert tool.input_model is BatchReadEight
        assert tool.definition.permissions == ("fs.read",)
        assert tool.definition.side_effects is False
        with pytest.raises(ToolError, match="path_denied"):
            await wide.call("read_many", {"paths": [*paths[:4], "../outside"]})
        mutation = await box.execute(
            (
                "python3",
                "-c",
                "from pathlib import Path; p=Path('/app/orders.py'); "
                "p.unlink(); p.symlink_to('/etc/passwd')",
            )
        )
        assert mutation.exit_code == 0
        with pytest.raises(ToolError, match="path_denied"):
            await wide.call("read_many", {"paths": [*paths]})


@pytest.mark.integration
async def test_limit_probe_uses_real_runtime_and_equal_public_information(tmp_path: Path) -> None:
    from minidsh_rl.minirepo_limit_probe import probe  # noqa: PLC0415

    await probe(tmp_path / "control")
    r = json.loads((tmp_path / "control/control-summary.json").read_text())
    assert r["passed"]
    assert r["same_observed_files"]
    assert r["new_model_calls"] == r["scientific_rollouts"] == 0
    assert r["arms"]["batch_read"]["oversized_request_rejections"] == 1
    assert r["arms"]["batch_read_eight"]["oversized_request_rejections"] == 0
    assert all(a["success"] and not a["pre_revision_private_passed"] for a in r["arms"].values())
