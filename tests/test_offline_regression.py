"""Contracts for the reproducible offline experiment entry point."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/offline_regression.py"
SPEC = importlib.util.spec_from_file_location("offline_regression", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
offline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(offline)


def test_experiment_environment_does_not_inherit_credentials_or_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-placeholder")
    monkeypatch.setenv("MINIDSH_MODEL_PROFILE", "deepseek")
    monkeypatch.setenv("HTTPS_PROXY", "http://example.invalid")
    monkeypatch.setenv("PATH", "/test/bin")
    environment = offline.clean_environment()
    assert environment["PATH"] == "/test/bin"
    assert "DEEPSEEK_API_KEY" not in environment
    assert "MINIDSH_MODEL_PROFILE" not in environment
    assert "HTTPS_PROXY" not in environment


def test_source_fingerprint_covers_scripts_and_fixtures_but_ignores_bytecode(
    tmp_path: Path,
) -> None:
    for directory in ("src", "scripts", "benchmarks/core/fixtures"):
        (tmp_path / directory).mkdir(parents=True)
        (tmp_path / directory / "example.py").write_text("original", encoding="utf-8")
    original = offline.source_fingerprint(tmp_path)
    cache = tmp_path / "src/__pycache__"
    cache.mkdir()
    (cache / "example.pyc").write_bytes(b"ignored")
    assert offline.source_fingerprint(tmp_path) == original
    (tmp_path / "scripts/example.py").write_text("changed", encoding="utf-8")
    assert offline.source_fingerprint(tmp_path) != original
    revised = offline.source_fingerprint(tmp_path)
    (tmp_path / "benchmarks/core/fixtures/example.py").write_text("changed", encoding="utf-8")
    assert offline.source_fingerprint(tmp_path) != revised


def test_repeat_audit_requires_all_runs_and_matching_deterministic_metrics() -> None:
    runs = [
        {"task_id": "task", "success": True, "tokens": 42, "latency_ms": 1.0},
        {"task_id": "task", "success": True, "tokens": 42, "latency_ms": 2.0},
    ]
    assert offline.compare_repeats(runs, repeats=2) == 1
    with pytest.raises(offline.RegressionError, match="repeat count"):
        offline.compare_repeats(runs[:1], repeats=2)
    runs[1]["tokens"] = 43
    with pytest.raises(offline.RegressionError, match="tokens"):
        offline.compare_repeats(runs, repeats=2)


def test_repeat_audit_rejects_failed_criteria_even_when_repeats_match() -> None:
    with pytest.raises(offline.RegressionError, match="criterion failed"):
        offline.compare_repeats([{"task_id": "task", "success": False}], repeats=1)


def test_failed_subcommand_cannot_be_treated_as_a_successful_experiment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["test-command"], 1)

    monkeypatch.setattr(offline.subprocess, "run", failed_run)
    with pytest.raises(offline.RegressionError, match="failed; inspect"):
        offline.run_command(["test-command"], tmp_path, "experiment")
