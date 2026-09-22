"""End-to-end benchmark runner test with an executable success criterion."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.benchmark import (
    BenchmarkError,
    BenchmarkManifest,
    BenchmarkReport,
    BenchmarkRunner,
    BenchmarkTask,
)
from minidsh.composition import LocalRuntime, RuntimeOptions, build_local_runtime
from minidsh.domain import ModelResponse, ToolCall
from minidsh.evaluation import TraceRun


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
        model={"label": "test-model", "pricing": {"input_usd_per_million": 3.0}},
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


def test_a_report_without_recorded_conditions_omits_the_key() -> None:
    """Absent is not the same claim as "no model".

    Reports written before the conditions block existed must stay readable, so
    the key is omitted rather than written as null — a reader can then tell
    "this run did not record its model" apart from "this run had none".
    """
    report = BenchmarkReport("smoke", "2.0", ())

    assert "model" not in json.loads(report.to_json())
    assert "## Model and rate card" not in report.to_markdown()


def test_a_report_records_the_model_and_the_rate_card_it_was_priced_with() -> None:
    """A cost number without its tariff is not a measurement.

    The rate card and the thinking flag are the two conditions a cost or
    accuracy figure depends on, and both used to live only in the shell command
    that produced the report.
    """
    conditions: dict[str, object] = {
        "label": "deepseek-flash",
        "pricing": {"cached_input_usd_per_million": 0.003, "input_usd_per_million": 0.15},
        "profile": {"profile": "deepseek", "temperature_effective": True, "thinking": False},
    }
    report = BenchmarkReport("mini-dsh-repo", "2.0", (), model=conditions)

    assert json.loads(report.to_json())["model"] == conditions

    markdown = report.to_markdown()
    assert "## Model and rate card" in markdown
    # Nested facts are flattened one level, so the two that decide whether a
    # quoted cost is meaningful are readable without parsing a JSON blob.
    assert "- `pricing.input_usd_per_million`: 0.15" in markdown
    assert "- `profile.thinking`: false" in markdown
    assert "- `profile.temperature_effective`: true" in markdown


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


# --- Held-out verification ---------------------------------------------------
#
# The `repo` suite verifies by re-running the tests the agent could read and edit,
# so it measures compliance with visible tests rather than correctness against a
# specification -- which is why it saturates at 100%.  A `verification_overlay`
# copies verifier files over the workspace *after* the agent finishes and *before*
# the success command runs, so the criterion is one the agent never saw.
#
# The three properties below are the whole contract, and each is separately
# breakable: an overlay applied before the run is readable and editable; one
# applied after verification is decorative; one that is declared but missing
# silently degrades the task back to the weak criterion it was meant to replace.

_PASSING_CHECK = "raise SystemExit(0)\n"
_REAL_CHECK = (
    "from pathlib import Path\n"
    "import sys\n"
    "sys.exit(0 if Path('value.txt').read_text() == 'fixed\\n' else 1)\n"
)


def _overlay_runner(
    tmp_path: Path, factory: Callable[[Path, BenchmarkTask], LocalRuntime], output: str
) -> BenchmarkRunner:
    """Build a runner that may execute the interpreter the fixture's checks use."""
    return BenchmarkRunner(
        factory,
        tmp_path / output,
        verification_allowlist=frozenset({Path(sys.executable).name}),
    )


def _idle_factory(workspace: Path, benchmark_task: BenchmarkTask) -> LocalRuntime:
    """A model that declares success without changing anything."""
    del benchmark_task
    return build_local_runtime(
        workspace=workspace,
        model=ScriptedModel([ModelResponse(content="done")]),
        options=RuntimeOptions(allow_write=True),
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_a_held_out_overlay_is_what_decides_the_outcome(tmp_path: Path) -> None:
    """Identical runs must score differently when only the verifier differs.

    The fixture's `check.py` always passes, which is what a task degenerates into
    when the criterion is the agent's own test suite: an agent that changes nothing
    and announces success is recorded as a success.  The overlay supplies the real
    check, and the same agent behaviour is then correctly recorded as a failure.
    Holding the agent fixed is the point -- if the two runs differed in what the
    model did, the difference in outcome would prove nothing about the verifier.
    """
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "value.txt").write_text("broken\n", encoding="utf-8")
    (fixture / "check.py").write_text(_PASSING_CHECK, encoding="utf-8")

    overlay = tmp_path / "verification"
    overlay.mkdir()
    (overlay / "check.py").write_text(_REAL_CHECK, encoding="utf-8")

    argv = (sys.executable, "check.py")
    gameable = BenchmarkTask("visible-only", "fix value.txt", fixture, argv)
    held_out = BenchmarkTask(
        "held-out", "fix value.txt", fixture, argv, verification_overlay=overlay
    )

    loose = await _overlay_runner(tmp_path, _idle_factory, "loose").run(
        BenchmarkManifest("loose", "2.1", (gameable,))
    )
    strict = await _overlay_runner(tmp_path, _idle_factory, "strict").run(
        BenchmarkManifest("strict", "2.1", (held_out,))
    )

    assert loose.results[0].success is True, (
        "a verifier that cannot fail is not a criterion; this is the saturation the "
        "overlay exists to remove"
    )
    assert strict.results[0].success is False
    assert strict.results[0].failure_type == "verification_failed"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_the_overlay_lands_after_the_agent_and_before_the_verifier(
    tmp_path: Path,
) -> None:
    """The copy must be late enough to be uneditable and early enough to count.

    The agent patches `check.py` from a placeholder into one that always passes.
    `patch_file` asserts its `expected` text, so that patch can only succeed if the
    agent saw the placeholder -- proving the overlay was *not* applied before the
    run.  The run is still scored a failure, because the overlay replaced the agent's
    edit before the verifier ran -- proving it *was* applied before verification.
    Both halves are needed: applying the overlay first makes the patch fail, and
    never applying it lets the agent's own test decide the outcome.
    """
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "value.txt").write_text("broken\n", encoding="utf-8")
    placeholder = "PLACEHOLDER = 'agent may edit this'\n" + _PASSING_CHECK
    (fixture / "check.py").write_text(placeholder, encoding="utf-8")

    overlay = tmp_path / "verification"
    overlay.mkdir()
    (overlay / "check.py").write_text(_REAL_CHECK, encoding="utf-8")

    def factory(workspace: Path, benchmark_task: BenchmarkTask) -> LocalRuntime:
        del benchmark_task
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "patch",
                            "patch_file",
                            {
                                "path": "check.py",
                                "expected": "PLACEHOLDER = 'agent may edit this'\n",
                                "replacement": "",
                            },
                        ),
                    )
                ),
                ModelResponse(content="the test passes now"),
            ]
        )
        return build_local_runtime(
            workspace=workspace, model=model, options=RuntimeOptions(allow_write=True)
        )

    task = BenchmarkTask(
        "gamed-verifier",
        "fix value.txt",
        fixture,
        (sys.executable, "check.py"),
        verification_overlay=overlay,
    )
    report = await _overlay_runner(tmp_path, factory, "out").run(
        BenchmarkManifest("gamed", "2.1", (task,))
    )
    metrics = report.results[0]
    trace = await asyncio.to_thread(TraceRun.from_file, Path(metrics.trace_path))
    statuses = {
        str(record.data.get("tool")): str(record.data.get("status"))
        for record in trace.by_name("tool_call_completed")
    }

    assert statuses == {"patch_file": "succeeded"}, (
        "the agent must have seen the visible placeholder; a patch that failed here "
        "means the overlay was applied before the run, which makes the verifier "
        "readable and editable"
    )
    assert metrics.success is False, (
        "the agent's rewrite of the verifier must not survive to verification"
    )
    assert metrics.failure_type == "verification_failed"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_a_manifest_declaring_a_missing_overlay_is_rejected(tmp_path: Path) -> None:
    """Fail loudly rather than quietly running a weaker task than the one declared.

    A missing overlay is not a crash but a silent downgrade: the task still runs, and
    it still reports a success rate, only now measured against the visible tests the
    agent could edit.  The number would be read as if the held-out criterion had been
    applied, so the run must not start.
    """
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "value.txt").write_text("broken\n", encoding="utf-8")
    task = BenchmarkTask(
        "missing-overlay",
        "fix value.txt",
        fixture,
        (sys.executable, "check.py"),
        verification_overlay=tmp_path / "nowhere",
    )

    with pytest.raises(BenchmarkError, match="verification overlay does not exist"):
        await _overlay_runner(tmp_path, _idle_factory, "out").run(
            BenchmarkManifest("broken", "2.1", (task,))
        )


def test_a_manifest_resolves_the_overlay_relative_to_itself(tmp_path: Path) -> None:
    """An overlay path is as relative as the workspace path beside it.

    Resolving it against the process working directory instead of the manifest would
    work from the repository root and fail everywhere else -- including from the
    command the docs tell a reader to run.
    """
    root = tmp_path / "benchmarks"
    (root / "fixtures" / "task").mkdir(parents=True)
    (root / "verification" / "task").mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "name": "hard",
                "schema_version": "2.1",
                "tasks": [
                    {
                        "id": "task",
                        "goal": "make it pass",
                        "workspace": "fixtures/task",
                        "success_argv": ["python", "check.py"],
                        "verification_overlay": "verification/task",
                        "verification_commit": "sha256:abc",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    manifest = BenchmarkManifest.from_file(root / "manifest.json")

    task = manifest.tasks[0]
    assert task.verification_overlay == (root / "verification" / "task").resolve()
    assert task.workspace == (root / "fixtures" / "task").resolve()
    assert task.verification_commit == "sha256:abc"


def test_a_manifest_without_an_overlay_leaves_it_unset(tmp_path: Path) -> None:
    """Absent must stay absent: the weaker criterion is not the default by accident.

    A task that declares no overlay is a visible-test task, and saying so with `None`
    keeps the two kinds of task distinguishable in a report.
    """
    root = tmp_path / "benchmarks"
    (root / "fixtures" / "task").mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "name": "repo",
                "schema_version": "2.0",
                "tasks": [
                    {
                        "id": "task",
                        "goal": "make it pass",
                        "workspace": "fixtures/task",
                        "success_argv": ["python", "check.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    manifest = BenchmarkManifest.from_file(root / "manifest.json")

    assert manifest.tasks[0].verification_overlay is None
    assert manifest.tasks[0].verification_commit is None
