"""Exercise architecture rules on isolated source trees without importing their code."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from minidsh.adapters.scoring_evidence import ScoringEvidence
from minidsh.evaluation.harness_metrics import ScoringEvidence as PublicEvidence

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_architecture.py"


def _source(root: Path, name: str, content: str) -> None:
    path = root / "src/minidsh" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--json"],
        cwd=root,
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_repository_obeys_layer_rules() -> None:
    result = _check(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_core_uses_ports_without_loading_project_code(tmp_path: Path) -> None:
    _source(tmp_path, "domain/models.py", "raise RuntimeError('must not execute')\n")
    _source(tmp_path, "ports/__init__.py", "from minidsh.domain.models import Message\n")
    _source(tmp_path, "application/loop.py", "from minidsh.ports import Model\n")
    result = _check(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["module_count"] == 3


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("domain/models.py", "minidsh.adapters.model"),
        ("ports/model.py", "minidsh.runtime.events"),
        ("application/loop.py", "minidsh.adapters.model"),
        ("runtime/events.py", "minidsh.tools.runtime"),
        ("adapters/model.py", "minidsh.evaluation.metrics"),
        ("infrastructure/process.py", "minidsh.application.loop"),
        ("evaluation/metrics.py", "minidsh.benchmark.runner"),
        ("composition.py", "minidsh.cli"),
        ("commands/reports.py", "minidsh.cli"),
        ("commands/__init__.py", "minidsh.cli"),
    ],
)
def test_forbidden_dependency_reports_location(tmp_path: Path, source: str, target: str) -> None:
    _source(tmp_path, source, f"import {target}\n")
    _source(tmp_path, target.removeprefix("minidsh.").replace(".", "/") + ".py", "")
    result = _check(tmp_path)
    assert result.returncode == 1
    assert '"code": "forbidden_dependency"' in result.stdout
    assert source in result.stdout
    assert '"line": 1' in result.stdout


def test_relative_imports_inside_functions_are_checked(tmp_path: Path) -> None:
    _source(tmp_path, "application/loop.py", "def run():\n    from ..adapters import model\n")
    _source(tmp_path, "adapters/model.py", "")
    result = _check(tmp_path)
    assert result.returncode == 1
    assert '"code": "forbidden_dependency"' in result.stdout
    assert '"line": 2' in result.stdout


def test_type_only_and_third_party_core_dependencies_are_checked(tmp_path: Path) -> None:
    _source(
        tmp_path,
        "domain/models.py",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import httpx\n",
    )
    result = _check(tmp_path)
    assert result.returncode == 1
    assert '"code": "external_core_dependency"' in result.stdout


def test_runtime_extensions_can_use_tool_contracts(tmp_path: Path) -> None:
    _source(tmp_path, "runtime/skills/service.py", "from minidsh.tools.runtime import ToolError\n")
    _source(tmp_path, "tools/runtime.py", "from minidsh.runtime.hooks import HookRegistry\n")
    _source(tmp_path, "runtime/hooks.py", "")
    result = _check(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_cycles_are_detected_even_inside_one_layer(tmp_path: Path) -> None:
    _source(tmp_path, "domain/a.py", "from .b import value\n")
    _source(tmp_path, "domain/b.py", "from .a import value\n")
    result = _check(tmp_path)
    assert result.returncode == 1
    assert json.loads(result.stdout)["cycles"] == [["minidsh.domain.a", "minidsh.domain.b"]]


def test_new_unclassified_module_fails_closed(tmp_path: Path) -> None:
    _source(tmp_path, "misc.py", "")
    result = _check(tmp_path)
    assert result.returncode == 1
    assert '"code": "unclassified_module"' in result.stdout


def test_missing_or_invalid_source_is_an_explicit_failure(tmp_path: Path) -> None:
    missing = _check(tmp_path)
    assert missing.returncode == 1
    assert '"code": "missing_source"' in missing.stdout
    _source(tmp_path, "domain/broken.py", "def broken(\n")
    invalid = _check(tmp_path)
    assert invalid.returncode == 1
    assert '"code": "syntax_error"' in invalid.stdout


def test_shared_evidence_schema_does_not_import_the_scorer() -> None:
    # Memory provenance checking must not load metric aggregation or benchmark runners.
    assert PublicEvidence is ScoringEvidence
    assert (
        ScoringEvidence.model_validate({"run_id": "r", "source": "verifier"}).task_success is None
    )

    isolated = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import minidsh.adapters.memory_evidence; "
            "assert not any(name.startswith('minidsh.evaluation') for name in sys.modules)",
        ],
        cwd=ROOT,
        env={"PATH": os.defpath, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert isolated.returncode == 0, isolated.stdout + isolated.stderr
