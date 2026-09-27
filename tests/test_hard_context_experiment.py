"""Contracts for the interleaved two-strategy experiment protocol."""

import importlib.util
from collections import Counter
from pathlib import Path

import pytest

from minidsh.benchmark.runner import BenchmarkManifest, BenchmarkReport, BenchmarkTask, TaskMetrics

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/hard_context_experiment.py"
SPEC = importlib.util.spec_from_file_location("hard_context_experiment", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


def test_schedule_balances_order_and_samples_without_changing_task_inputs() -> None:
    tasks = tuple(
        BenchmarkTask(str(index), "goal", Path("fixture"), ("python",)) for index in range(4)
    )
    schedule = experiment.schedule_trials(tasks, repeats=3)
    assert len(schedule) == 24
    assert Counter((task.task_id, variant.name) for _, task, variant in schedule) == {
        (str(index), name): 3
        for index in range(4)
        for name in ("full_history", "summary_compaction")
    }
    assert Counter(variant.name for _, _, variant in schedule[::2]) == {
        "full_history": 6,
        "summary_compaction": 6,
    }
    assert all(task is tasks[int(task.task_id)] for _, task, _ in schedule)
    assert schedule == experiment.schedule_trials(tasks, repeats=3)


def test_schedule_rejects_empty_tasks_and_invalid_repeats() -> None:
    task = BenchmarkTask("task", "goal", Path("fixture"), ("python",))
    with pytest.raises(ValueError, match="positive"):
        experiment.schedule_trials((task,), repeats=0)
    with pytest.raises(ValueError, match="empty"):
        experiment.schedule_trials((), repeats=3)


@pytest.mark.asyncio
async def test_interleaved_reports_preserve_trial_indices_and_failed_criteria() -> None:
    task = BenchmarkTask("task", "goal", Path("fixture"), ("python",))
    manifest = BenchmarkManifest("suite", "2.0", (task,))

    class FakeRunner:
        async def run(self, selected: BenchmarkManifest) -> BenchmarkReport:
            assert selected.tasks == (task,)
            result = TaskMetrics(
                task_id="task",
                success=False,
                status="finished",
                steps=1,
                tokens=0,
                cost_usd=0,
                tool_calls=0,
                latency_ms=0,
                failure_type="verification_failed",
                verifier_exit_code=1,
                trace_path="trace",
            )
            return BenchmarkReport("suite", "2.0", (result,))

    def factory(*_args: object) -> FakeRunner:
        return FakeRunner()

    report = await experiment.run_trials(manifest, repeats=3, runner_factory=factory)
    assert len(report.variants) == 2
    for variant in report.variants:
        assert [result.run_index for result in variant.report.results] == [0, 1, 2]
        assert variant.report.aggregate()["success_rate"] == 0


@pytest.mark.asyncio
async def test_interleaved_reports_reject_missing_task_results() -> None:
    task = BenchmarkTask("task", "goal", Path("fixture"), ("python",))
    manifest = BenchmarkManifest("suite", "2.0", (task,))

    class EmptyRunner:
        async def run(self, selected: BenchmarkManifest) -> BenchmarkReport:
            return BenchmarkReport(selected.name, selected.schema_version, ())

    def factory(*_args: object) -> EmptyRunner:
        return EmptyRunner()

    with pytest.raises(ValueError, match="one matching task"):
        await experiment.run_trials(manifest, repeats=1, runner_factory=factory)
