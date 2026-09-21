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
