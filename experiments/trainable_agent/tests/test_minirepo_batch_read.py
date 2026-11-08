"""Batch reads preserve the existing information and path-security boundaries."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from minidsh_rl import minirepo_experiment as experiment
from minidsh_rl.minirepo_analysis import feedback_revision_indices
from minidsh_rl.minirepo_experiment import batch_evaluation_queue
from minidsh_rl.minirepo_tasks import development_tasks
from minidsh_rl.minirepo_tools import BatchRead, RepoSession, RepoTool
from minidsh_rl.minirepo_verification import IMAGE, seed
from pydantic import ValidationError

from minidsh.infrastructure.containers import ContainerResources, DockerHost, DockerSandbox
from minidsh.infrastructure.process import CommandPolicy, ProcessResult
from minidsh.tools.runtime import ToolError


@pytest.mark.parametrize("condition", ["valid", "late_batch", "partial_already_passes"])
async def test_batch_mechanism_gate_requires_early_batch_and_independent_partial_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
) -> None:
    """Synthetic verdict doubles test scoring contracts, never model efficacy."""
    from minidsh_rl import minirepo_analysis as scoring  # noqa: PLC0415

    async def verifier(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(passed=condition == "partial_already_passes")

    monkeypatch.setattr(scoring, "verify", verifier)
    queue = batch_evaluation_queue()
    selected_cases = {development_tasks()[0].case_id, development_tasks()[2].case_id}
    task_map = {t.case_id: t for t in development_tasks()}
    for index, row in enumerate(queue):
        row.update(run_id=f"synthetic_{index}", status="completed")
        root = tmp_path / str(row["run_id"])
        root.mkdir()
        success = row["read_variant"] == "batch_read" and row["case_id"] in selected_cases
        result = row | {
            "success": success,
            "total_tokens": 0,
            "generated_tokens": 0,
            "sampled_calls": 0,
            "patches": 3 if success else 0,
            "tool_calls": 5 if success else 0,
            "duration_ms": 1,
            "batch_read_calls": 1 if success else 0,
        }
        operations = []
        snapshots = []
        if success:
            task = task_map[str(row["case_id"])]
            candidate = dict(task.reference)
            digest = hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()
            result["candidate_hash"] = digest
            (root / "candidate.json").write_text(json.dumps(candidate))
            (root / "verification").mkdir()
            (root / "verification/result.json").write_text(
                json.dumps({"candidate_sha256": digest, "passed": True})
            )
            snapshots = [dict(task.files), dict(task.files), dict(task.files) | candidate]
            operations = [
                {"operation": "patch", "patches": 1},
                {"operation": "patch", "patches": 2},
                {"operation": "test", "patches": 2, "exit_code": 1},
                {"operation": "patch", "patches": 3},
            ]
            operations.insert(
                3 if condition == "late_batch" else 2,
                {"operation": "read_many", "patches": 2},
            )
        (root / "result.json").write_text(json.dumps(result))
        (root / "operations.json").write_text(json.dumps(operations))
        (root / "patch-snapshots.json").write_text(json.dumps(snapshots))
    (tmp_path / "queue.json").write_text(json.dumps(queue))
    (tmp_path / "protocol.json").write_text(
        json.dumps({"phase": "batch_read_development", "declared": 32})
    )
    output = tmp_path / "analysis.json"
    await scoring.analyze(tmp_path, output)
    report = json.loads(output.read_text())
    assert report["policies"]["batch_read"]["successes"] == 4
    assert report["interaction_signal_gate"] is (condition == "valid")
    assert len(report["feedback_revision_families"]) == (2 if condition == "valid" else 0)
    assert len(report["batch_feedback_revision_cases"]) == 4


class NeverExecute:
    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str = ".",
        timeout_seconds: float = 30.0,
    ) -> ProcessResult:
        del argv, cwd, timeout_seconds
        pytest.fail("invalid batch must be rejected before a process starts")


@pytest.mark.parametrize("defect", ["missing", "model_calls", "content", "source"])
async def test_batch_admission_rejects_missing_or_stale_control_before_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("invalid control must be rejected before connecting to services")

    monkeypatch.setattr("minidsh_rl.minirepo_experiment.trio.ServiceClient", forbidden)
    monkeypatch.setattr("minidsh_rl.minirepo_experiment.MetricRecorder.connect", forbidden)
    gates = tmp_path / "gates.json"
    gates.write_text(
        json.dumps({"passed": True, "completed": 56, "image": IMAGE, "source_sha256": {}})
    )
    source = tmp_path / "source.py"
    source.write_text("changed")
    control = tmp_path / "control.json"
    control.write_text(
        json.dumps(
            {
                "passed": True,
                "new_model_calls": 1 if defect == "model_calls" else 0,
                "same_observed_files": defect != "content",
                "source_sha256": {str(source): "stale"} if defect == "source" else {},
            }
        )
    )
    with pytest.raises(ValueError, match="batch"):
        await experiment.evaluate(
            tmp_path / "rejected",
            gates,
            tmp_path / "unused-quote.json",
            mode="online",
            max_completion_per_call=1024,
            batch_study=True,
            batch_control=None if defect == "missing" else control,
        )
    assert not (tmp_path / "rejected").exists()


def test_batch_queue_keeps_complete_pairs_and_original_prompt() -> None:
    rows = batch_evaluation_queue()
    assert len(rows) == 32
    for index in range(0, len(rows), 2):
        a, b = rows[index : index + 2]
        assert (a["case_id"], a["seed"], a["repeat"]) == (b["case_id"], b["seed"], b["repeat"])
        assert {a["read_variant"], b["read_variant"]} == {"single_read", "batch_read"}
        assert a["prompt_variant"] == b["prompt_variant"] == "original"
        assert a["mode"] == b["mode"] == "interactive"


def test_feedback_revision_can_follow_second_patch_and_rejects_counter_gaps() -> None:
    operations: list[dict[str, object]] = [
        {"operation": "patch", "patches": 1},
        {"operation": "patch", "patches": 2},
        {"operation": "test", "patches": 2, "exit_code": 1},
        {"operation": "read", "patches": 2},
        {"operation": "patch", "patches": 3},
        {"operation": "test", "patches": 3, "exit_code": 0},
    ]
    assert feedback_revision_indices(operations) == (2,)
    assert feedback_revision_indices(operations[:-2]) == ()
    assert feedback_revision_indices(operations, require_batch_before_feedback=True) == ()
    batch = {"operation": "read_many", "patches": 2}
    assert feedback_revision_indices(
        [*operations[:2], batch, *operations[2:]], require_batch_before_feedback=True
    ) == (2,)
    assert (
        feedback_revision_indices(
            [*operations[:3], batch, *operations[3:]], require_batch_before_feedback=True
        )
        == ()
    )
    with pytest.raises(ValueError, match="patch counter"):
        feedback_revision_indices(operations[1:])


@pytest.mark.parametrize("paths", [[], ["x"] * 5, ["x", "x"], [""], [3]])
def test_batch_schema_rejects_invalid_lists(paths: list[object]) -> None:
    with pytest.raises(ValidationError):
        BatchRead.model_validate({"paths": paths})


async def test_batch_preflight_rejects_whole_request_and_declares_read_only() -> None:
    session = RepoSession(NeverExecute(), development_tasks()[0])
    for paths in (["orders.py", "../outside"], ["/etc/passwd"], ["missing.py"]):
        with pytest.raises(ToolError, match="path_denied"):
            await session.call("read_many", {"paths": [*paths]})
    with pytest.raises(ToolError, match="invalid_arguments"):
        await session.call("read_many", {"paths": []})
    tool = RepoTool(session, "read_files")
    assert tool.definition.permissions == ("fs.read",)
    assert tool.definition.side_effects is False


@pytest.mark.integration
async def test_batch_matches_individual_reads_and_preserves_workspace() -> None:
    task = development_tasks()[0]
    async with DockerSandbox(
        DockerHost(),
        image=IMAGE,
        resources=ContainerResources(platform="linux/arm64", cpus=1, memory_mb=256),
    ) as box:
        await seed(box, dict(task.files))
        session = RepoSession(box.runner("/app", CommandPolicy(frozenset({"python3"}))), task)
        before = await session.capture()
        batch = await session.call("read_many", {"paths": ["orders.py", "reservation.py"]})
        assert isinstance(batch, dict)
        assert batch["truncated"] is False
        files = batch["files"]
        assert isinstance(files, list)
        for item in files:
            assert isinstance(item, dict)
            assert item == await session.call("read", {"path": item["path"]})
        assert await session.capture() == before
        assert session.patches == 0
        assert session.operations[-1]["operation"] == "read"


@pytest.mark.integration
async def test_batch_aggregate_truncation_is_explicit_and_symlinks_are_denied() -> None:
    task = development_tasks()[0]
    task = replace(task, files=(*task.files, ("note.txt", "a" * 6001)))
    async with DockerSandbox(
        DockerHost(),
        image=IMAGE,
        resources=ContainerResources(platform="linux/arm64", cpus=1, memory_mb=256),
    ) as box:
        await seed(box, dict(task.files))
        session = RepoSession(box.runner("/app", CommandPolicy(frozenset({"python3"}))), task)
        batch = await session.call("read_many", {"paths": ["note.txt", "orders.py"]})
        assert isinstance(batch, dict)
        assert batch["truncated"] is True
        assert batch["returned_characters"] == 5000
        files = batch["files"]
        assert isinstance(files, list)
        assert len(files) == 2
        assert all(isinstance(item, dict) and item["truncated"] is True for item in files)
        assert isinstance(files[1], dict)
        assert files[1]["content"] == ""
        individual = await session.call("read", {"path": "orders.py"})
        assert isinstance(individual, dict)
        assert individual["content"] == dict(task.files)["orders.py"]
        result = await box.execute(
            (
                "python3",
                "-c",
                "from pathlib import Path;p=Path('/app/orders.py');"
                "p.unlink();p.symlink_to('/etc/passwd')",
            )
        )
        assert result.exit_code == 0
        with pytest.raises(ToolError, match="path_denied"):
            await session.call("read_many", {"paths": ["reservation.py", "orders.py"]})


@pytest.mark.integration
async def test_whole_loop_control_preserves_information_and_advances_feedback(
    tmp_path: Path,
) -> None:
    from minidsh_rl.minirepo_batch_probe import probe  # noqa: PLC0415

    await probe(tmp_path / "control")
    record = json.loads((tmp_path / "control/control-summary.json").read_text())
    assert record["passed"] is True
    assert record["same_observed_files"] is True
    assert record["new_model_calls"] == 0
    assert record["arms"]["single_read"]["first_failed_test_step"] == 10
    assert record["arms"]["batch_read"]["first_failed_test_step"] == 4
    assert record["arms"]["single_read"]["success"] is True
    assert record["arms"]["batch_read"]["success"] is True
