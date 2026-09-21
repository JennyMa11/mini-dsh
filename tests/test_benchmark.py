"""End-to-end benchmark runner test with an executable success criterion."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.benchmark import BenchmarkManifest, BenchmarkRunner, BenchmarkTask
from minidsh.composition import LocalRuntime, RuntimeOptions, build_local_runtime
from minidsh.domain import ModelResponse, ToolCall


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_benchmark_runs_agent_in_copied_workspace(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "value.txt").write_text("broken\n", encoding="utf-8")
    output = tmp_path / "results"
    task = BenchmarkTask(
        "repair-value",
        "change broken to fixed",
        fixture,
        (
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "raise SystemExit(Path('value.txt').read_text() != 'fixed\\n')",
        ),
    )
    manifest = BenchmarkManifest("smoke", "1.0", (task,))

    def factory(workspace: Path, benchmark_task: BenchmarkTask) -> LocalRuntime:
        del benchmark_task
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "patch",
                            "patch_file",
                            {"path": "value.txt", "expected": "broken", "replacement": "fixed"},
                        ),
                    )
                ),
                ModelResponse(content="done"),
            ]
        )
        return build_local_runtime(
            workspace=workspace,
            model=model,
            options=RuntimeOptions(allow_write=True),
        )

    runner = BenchmarkRunner(
        factory,
        output,
        verification_allowlist=frozenset({Path(sys.executable).name}),
    )
    report = await runner.run(manifest)

    assert report.aggregate()["success_rate"] == 1.0
    assert report.results[0].success is True
    trace_exists = await asyncio.to_thread(Path(report.results[0].trace_path).is_file)
    assert trace_exists


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_repeats_are_recorded_and_aggregated(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "value.txt").write_text("broken\n", encoding="utf-8")
    output = tmp_path / "results"
    task = BenchmarkTask(
        "repair-value",
        "change broken to fixed",
        fixture,
        (
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "raise SystemExit(Path('value.txt').read_text() != 'fixed\\n')",
        ),
    )
    manifest = BenchmarkManifest("repeated", "2.0", (task,))

    def factory(workspace: Path, benchmark_task: BenchmarkTask) -> LocalRuntime:
        del benchmark_task
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "patch",
                            "patch_file",
                            {"path": "value.txt", "expected": "broken", "replacement": "fixed"},
                        ),
                    )
                ),
                ModelResponse(content="done"),
            ]
        )
        return build_local_runtime(
            workspace=workspace,
            model=model,
            options=RuntimeOptions(allow_write=True),
        )

    runner = BenchmarkRunner(
        factory,
        output,
        verification_allowlist=frozenset({Path(sys.executable).name}),
        repeats=3,
        model_label="test-model",
    )
    report = await runner.run(manifest)

    aggregate = report.aggregate()
    assert aggregate["tasks"] == 1
    assert aggregate["runs"] == 3
    assert [result.run_index for result in report.results] == [0, 1, 2]
    assert {result.model for result in report.results} == {"test-model"}
    assert report.repeat_variance()["repeated_tasks"] == 1.0
    assert report.repeat_variance()["success_spread"] == 0.0
    assert aggregate["cost_per_success_usd"] == 0.0


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_every_run_carries_a_taxonomy_and_latency_breakdown(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "value.txt").write_text("broken\n", encoding="utf-8")
    output = tmp_path / "results"
    task = BenchmarkTask(
        "repair-value",
        "change broken to fixed",
        fixture,
        (
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "raise SystemExit(Path('value.txt').read_text() != 'fixed\\n')",
        ),
    )
    manifest = BenchmarkManifest("classified", "2.0", (task,))

    def factory(workspace: Path, benchmark_task: BenchmarkTask) -> LocalRuntime:
        del benchmark_task
        model = ScriptedModel(
            [
                ModelResponse(content="I am done without changing anything."),
            ]
        )
        return build_local_runtime(
            workspace=workspace,
            model=model,
            options=RuntimeOptions(allow_write=True),
        )

    runner = BenchmarkRunner(
        factory,
        output,
        verification_allowlist=frozenset({Path(sys.executable).name}),
    )
    report = await runner.run(manifest)
    metrics = report.results[0]

    assert metrics.success is False
    assert metrics.failure_type == "verification_failed"
    assert metrics.failure_category == "verification_failed"
    assert metrics.failure_layer == "agent"
    assert metrics.harness_ms > 0.0
    assert report.failure_layer_counts() == {
        "success": 0,
        "harness": 0,
        "agent": 1,
        "environment": 0,
    }
    assert "## Outcome by layer" in report.to_markdown()


def test_repo_manifest_is_provider_only_and_has_no_response_scripts() -> None:
    manifest = BenchmarkManifest.from_file(Path("benchmarks/repo/manifest.json"))

    assert manifest.name == "mini-dsh-repo"
    assert len(manifest.tasks) == 6
    assert {task.category for task in manifest.tasks} == {
        "repo-bug-fix",
        "repo-callsite",
        "repo-test-repair",
        "repo-feature",
        "repo-config",
        "repo-error-recovery",
    }
    for task in manifest.tasks:
        assert task.response_script is None, "a canned response would measure the script"
        assert task.workspace.is_dir()
        assert task.success_argv
        assert task.repo_commit is not None
        assert task.repo_commit.startswith("sha256:")
