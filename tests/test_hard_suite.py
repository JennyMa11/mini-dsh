"""Gate the hard suite: every task must be both a real task and a solvable one.

The hard suite exists because every other suite verifies against tests the agent can
read and edit, so they saturate.  That makes two failure modes fatal, and both are
silent without a gate:

* A fixture that already passes measures nothing.  The task looks like a task, the
  success rate looks like a success rate, and no work was ever required.
* A fixture whose held-out checks cannot be satisfied by any solution turns a low
  success rate into an uninterpretable number.  A 0% result then cannot be told apart
  from a broken task, which is worse than not running the suite at all.

So every task is run twice, offline, with no model and no network: once as shipped,
where the verifier must fail, and once with the committed reference solution applied,
where it must pass.  The visible smoke subset is checked in both directions too, so
"the smoke subset is a subset of the contract" is a measured fact rather than a claim
in a docstring.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from minidsh.benchmark import BenchmarkManifest, BenchmarkTask
from minidsh.benchmark.runner import _apply_overlay

REPO_ROOT = Path(__file__).resolve().parent.parent
HARD = REPO_ROOT / "benchmarks" / "hard"
GENERATOR = REPO_ROOT / "benchmarks" / "build_hard.py"
TIMEOUT_SECONDS = 120


def _load_generator() -> types.ModuleType:
    """Import the generator by path so its digest helper is the one under test."""
    spec = importlib.util.spec_from_file_location("build_hard_gate", GENERATOR)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest() -> BenchmarkManifest:
    return BenchmarkManifest.from_file(HARD / "manifest.json")


def _read_tree(base: Path) -> dict[str, str]:
    """Return every committed file under ``base`` keyed by its relative path."""
    return {
        path.relative_to(base).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(base.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def _environment() -> dict[str, str]:
    """A clean environment, so an inherited PYTHONPATH cannot satisfy an import.

    The fixtures' own modules must be found through the workspace, which is exactly
    what would otherwise be papered over by the repository's `src` directory already
    being importable.
    """
    return {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}


def _run(argv: list[str], workspace: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env=_environment(),
        check=False,
    )


def _verification_argv(task: BenchmarkTask) -> list[str]:
    """The manifest's success command, run under the interpreter already in use.

    The manifest says `python` because that is what the CLI passes and what the
    runner's allowlist is written against.  The gate should not additionally depend
    on a `python` executable existing on PATH.
    """
    argv = list(task.success_argv)
    if argv and argv[0] == "python":
        argv[0] = sys.executable
    return argv


def _visible_argv() -> list[str]:
    return [sys.executable, "-m", "unittest", "discover", "-s", "."]


def _stage(tmp_path: Path, task: BenchmarkTask, name: str = "") -> Path:
    """Copy the task's workspace somewhere disposable.

    The overlay is *not* applied here.  It lands in the workspace only after the agent
    has finished, and the two directions below depend on that ordering: applying it up
    front would also drop `verification_tests/` into reach of the visible smoke run.
    """
    workspace = tmp_path / (name or task.task_id)
    shutil.copytree(task.workspace, workspace)
    return workspace


def _stage_verified(
    tmp_path: Path, task: BenchmarkTask, name: str, *, solution: bool = False
) -> Path:
    """Stage a workspace in the state the verifier is handed: after the overlay lands.

    Order mirrors the runner: the agent's work is already in place, then the overlay
    is copied over it.  The two file sets are disjoint, but reproducing the order
    keeps this honest about what is being verified.
    """
    overlay = task.verification_overlay
    assert overlay is not None
    workspace = _stage(tmp_path, task, name)
    if solution:
        _apply_solution(workspace, task.task_id)
    _apply_overlay(overlay, workspace)
    return workspace


def _apply_solution(workspace: Path, task_id: str) -> None:
    """Copy the committed reference solution over a staged workspace."""
    solution = HARD / "solutions" / task_id
    for path in sorted(solution.rglob("*")):
        if not path.is_file():
            continue
        destination = workspace / path.relative_to(solution)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def _hard_tasks() -> list[BenchmarkTask]:
    return list(_manifest().tasks)


def test_the_hard_suite_is_held_out_and_provider_only() -> None:
    """Every task must carry a held-out verifier and no canned response.

    A task without an overlay verifies against the visible tests, which is the
    saturation this suite was built to remove; a task with a response script measures
    the script instead of the model.
    """
    tasks = _hard_tasks()

    assert tasks, "the hard suite is empty"
    for task in tasks:
        assert task.response_script is None, (
            f"{task.task_id}: a canned response measures the script"
        )
        assert task.verification_overlay is not None, f"{task.task_id}: no held-out verifier"
        assert task.verification_overlay.is_dir(), f"{task.task_id}: overlay is missing"
        assert task.workspace.is_dir(), f"{task.task_id}: workspace is missing"
        assert task.model == "provider", f"{task.task_id}: the hard suite needs a real model"
        assert task.repo_commit is not None
        assert task.verification_commit is not None


def test_every_hard_task_fails_before_any_work(tmp_path: Path) -> None:
    """A task that already passes is not a task.

    Both the visible smoke subset and the held-out verifier must fail on the shipped
    fixture.  If the verifier passed here, every success rate the suite reports would
    be inflated by a task that required no work.

    Each held-out run asserts that discovery actually happened, because a verifier that
    never started -- a missing script, a broken import -- also exits non-zero, and
    would make this test pass for entirely the wrong reason.
    """
    for task in _hard_tasks():
        visible = _run(_visible_argv(), _stage(tmp_path, task, f"{task.task_id}-visible"))
        assert visible.returncode != 0, (
            f"{task.task_id}: the visible smoke subset already passes, so the task "
            f"starts solved\n{visible.stdout[-2000:]}"
        )

        workspace = _stage_verified(tmp_path, task, f"{task.task_id}-heldout")
        held_out = _run(_verification_argv(task), workspace)
        assert "collected" in held_out.stdout, (
            f"{task.task_id}: the held-out verifier did not run, so its exit code says "
            f"nothing about the task\n{held_out.stdout[-2000:]}{held_out.stderr[-2000:]}"
        )
        assert held_out.returncode != 0, (
            f"{task.task_id}: the held-out verifier already passes on the shipped "
            f"fixture\n{held_out.stdout[-2000:]}"
        )


def test_every_hard_task_passes_with_its_reference_solution(tmp_path: Path) -> None:
    """Without this, a low success rate cannot be told apart from a broken fixture.

    The reference solution must satisfy the visible smoke subset *and* the held-out
    verifier.  The first half is what makes the smoke subset a genuine subset of the
    contract rather than a contradicting one; the second half is what makes the task
    solvable at all.
    """
    for task in _hard_tasks():
        visible_workspace = _stage(tmp_path, task, f"{task.task_id}-visible")
        _apply_solution(visible_workspace, task.task_id)
        visible = _run(_visible_argv(), visible_workspace)
        assert visible.returncode == 0, (
            f"{task.task_id}: the reference solution does not satisfy the visible "
            f"smoke subset, so the two disagree about the contract\n"
            f"{visible.stdout[-2000:]}{visible.stderr[-2000:]}"
        )

        workspace = _stage_verified(tmp_path, task, f"{task.task_id}-heldout", solution=True)
        held_out = _run(_verification_argv(task), workspace)
        assert held_out.returncode == 0, (
            f"{task.task_id}: the committed reference solution fails the held-out "
            f"verifier, so a low success rate here would be uninterpretable\n"
            f"{held_out.stdout[-2000:]}{held_out.stderr[-2000:]}"
        )


def test_a_verifier_refuses_a_workspace_whose_checks_were_removed(tmp_path: Path) -> None:
    """Discovery must not be able to succeed vacuously.

    `unittest discover` exits 0 when it finds nothing, so a verifier written that way
    would pass a workspace whose test directory had been emptied -- the cheapest
    possible way to game a benchmark.  Both shapes of that are covered: the directory
    deleted, and the directory present but holding no checks.
    """
    task = _hard_tasks()[0]

    deleted = _stage_verified(tmp_path, task, "deleted", solution=True)
    shutil.rmtree(deleted / "verification_tests")
    result = _run(_verification_argv(task), deleted)
    assert result.returncode == 2, "a missing held-out suite must not count as a pass"
    assert "check directory is missing" in result.stderr

    emptied = _stage_verified(tmp_path, task, "emptied", solution=True)
    shutil.rmtree(emptied / "verification_tests")
    (emptied / "verification_tests").mkdir()
    # The package marker is left behind on purpose: `discover` refuses an unimportable
    # start directory with a traceback, which would exit non-zero for the wrong reason
    # and hide whether the count guard works.
    (emptied / "verification_tests" / "__init__.py").write_text("", encoding="utf-8")
    result = _run(_verification_argv(task), emptied)
    assert result.returncode == 2, "an empty held-out suite must not count as a pass"
    assert "fewer checks were collected" in result.stderr


def test_every_hard_task_declares_the_checks_it_ships() -> None:
    """The declared minimum must equal what the overlay actually contains.

    The minimum is the only thing standing between the suite and a vacuous pass, so a
    stale number would quietly disable that guard.  Re-deriving it from the committed
    overlay catches a hand-edited test file that the manifest was not regenerated for.
    """
    for task in _hard_tasks():
        assert task.verification_overlay is not None
        overlay = _read_tree(task.verification_overlay)
        actual = sum(
            line.strip().startswith("def test_")
            for content in overlay.values()
            for line in content.splitlines()
        )
        declared = int(task.success_argv[-1])
        assert declared == actual, (
            f"{task.task_id}: the manifest declares {declared} checks but the overlay "
            f"contains {actual}"
        )
        assert actual > 0, f"{task.task_id}: the overlay ships no checks"


def test_the_committed_artifacts_match_the_generator() -> None:
    """The generator is the source of truth; a hand-edited fixture must not survive.

    `repo_commit` and `verification_commit` pin the fixture and the overlay, so
    recomputing them from the committed files and comparing is what keeps them
    meaningful.  Without this check the digests would pin whatever was committed
    first and quietly stop describing anything.
    """
    generator = _load_generator()

    for task in _hard_tasks():
        assert task.verification_overlay is not None
        workspace_digest = generator._digest(_read_tree(task.workspace))
        assert workspace_digest == task.repo_commit, (
            f"{task.task_id}: the committed fixture no longer matches its recorded "
            f"digest; run `python benchmarks/build_hard.py`"
        )
        overlay_digest = generator._digest(_read_tree(task.verification_overlay))
        assert overlay_digest == task.verification_commit, (
            f"{task.task_id}: the committed overlay no longer matches its recorded "
            f"digest; run `python benchmarks/build_hard.py`"
        )


@pytest.mark.integration
def test_the_generator_reproduces_the_committed_tree(tmp_path: Path) -> None:
    """Regenerating must be a no-op, so the artifacts are reproducible.

    Digests cover the manifest's own claims; this covers everything else the generator
    writes -- the solution trees, the file set, and the manifest itself.  It is also
    the check that would catch a fixture edited by hand in a directory the manifest
    does not pin.
    """
    generator = _load_generator()
    regenerated_root = tmp_path / "hard"
    generator.main(regenerated_root)

    regenerated = {
        path.relative_to(regenerated_root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(regenerated_root.rglob("*"))
        if path.is_file()
    }
    committed = {
        path.relative_to(HARD).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(HARD.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }

    assert set(regenerated) == set(committed), (
        "the generator writes a different set of files than is committed: "
        f"only regenerated {sorted(set(regenerated) - set(committed))}, "
        f"only committed {sorted(set(committed) - set(regenerated))}"
    )
    differing = sorted(name for name in committed if committed[name] != regenerated.get(name))
    assert not differing, (
        f"these committed files differ from a fresh run of the generator: {differing}"
    )
