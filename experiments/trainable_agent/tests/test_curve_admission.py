"""Synthetic evidence tests bind every probe/verdict before any SDK factory."""

import json
from pathlib import Path

import pytest
from minidsh_rl.curve_cases_v2 import curve_cases_v2
from minidsh_rl.curve_run import validate_data
from minidsh_rl.learning_rollout import _case_metadata, turn_digest
from minidsh_rl.learning_schedule import digest
from minidsh_rl.learning_tasks import learning_probes


def evidence(root: Path) -> Path:
    cases = curve_cases_v2()
    artifacts = {}
    rows = []
    for probe in learning_probes(cases):
        check = {
            "passed": probe.expected_correct,
            "exit_code": 0 if probe.expected_correct else 1,
            "timed_out": False,
        }
        candidate = turn_digest(dict(probe.candidate))
        row = {
            "case_id": probe.case_id,
            "probe_id": probe.probe_id,
            "expected_correct": probe.expected_correct,
            "kind": probe.kind,
            "candidate_sha256": candidate,
            "public": check,
            "private": check,
        }
        rows.append(row)
        for mode in ("public", "private"):
            path = root / f"{probe.case_id}__{probe.probe_id}" / mode / "result.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"case_id": probe.case_id, "candidate_sha256": candidate, **check})
            )
            artifacts[str(path)] = digest(path)
    record = {
        "completed_checks": 420,
        "declared_checks": 420,
        "candidate_policy": "curve_pure_v1",
        "source_unchanged": True,
        "rows": rows,
        "verdict_artifact_sha256": artifacts,
        "case_bindings": {
            case.task.case_id: _case_metadata(case).model_dump(mode="json") for case in cases
        },
    }
    path = root / "summary.json"
    path.write_text(json.dumps(record))
    return path


def test_complete_evidence_and_forged_summary(tmp_path: Path) -> None:
    path = evidence(tmp_path)
    validate_data(path)
    record = json.loads(path.read_text())
    record["rows"][0]["candidate_sha256"] = "0" * 64
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="probe identity"):
        validate_data(path)


def test_changed_verdict_and_changed_checker(tmp_path: Path) -> None:
    path = evidence(tmp_path)
    record = json.loads(path.read_text())
    verdict = Path(next(iter(record["verdict_artifact_sha256"])))
    verdict.write_text("{}")
    with pytest.raises(ValueError, match="evidence changed"):
        validate_data(path)
