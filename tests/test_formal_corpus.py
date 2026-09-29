"""Offline validity gate for every frozen formal task."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from minidsh.benchmark.formal import frozen_tree_digest
from minidsh.benchmark.runner import BenchmarkManifest, BenchmarkTask

ROOT = Path(__file__).resolve().parent.parent / "benchmarks" / "local_formal" / "v1"
MANIFEST = BenchmarkManifest.from_file(ROOT / "manifest.json")


def _check(
    task: BenchmarkTask, tmp_path: Path, *, solved: bool
) -> subprocess.CompletedProcess[str]:
    workspace = tmp_path / ("solved" if solved else "broken")
    shutil.copytree(task.workspace, workspace)
    if solved:
        solution = ROOT / "solutions" / task.task_id
        for source in solution.rglob("*"):
            if source.is_file():
                target = workspace / source.relative_to(solution)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    assert task.verification_overlay is not None
    shutil.copytree(task.verification_overlay, workspace, dirs_exist_ok=True)
    argv = [sys.executable, *task.success_argv[1:]]
    return subprocess.run(
        argv,
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
    )


def test_formal_corpus_has_declared_category_counts_and_pins() -> None:
    assert len(MANIFEST.tasks) == 26
    counts: dict[str, int] = {}
    for task in MANIFEST.tasks:
        counts[task.category] = counts.get(task.category, 0) + 1
        assert task.response_script is None
        assert task.verification_overlay is not None
        assert task.repo_commit == frozen_tree_digest(task.workspace)
        assert task.verification_commit == frozen_tree_digest(task.verification_overlay)
        assert task.solution_commit == frozen_tree_digest(ROOT / "solutions" / task.task_id)
        assert (ROOT / "solutions" / task.task_id).is_dir()
    assert counts == {
        "bug_fix": 8,
        "small_feature": 6,
        "refactor": 4,
        "test_repair": 4,
        "repo_navigation": 4,
    }


@pytest.mark.parametrize("task", MANIFEST.tasks, ids=lambda task: task.task_id)
def test_broken_fails_and_reference_passes(task: BenchmarkTask, tmp_path: Path) -> None:
    broken = _check(task, tmp_path, solved=False)
    solved = _check(task, tmp_path, solved=True)
    assert broken.returncode != 0, f"{task.task_id}: broken fixture unexpectedly passed"
    assert solved.returncode == 0, (
        f"{task.task_id}: reference failed\n{solved.stdout}\n{solved.stderr}"
    )
