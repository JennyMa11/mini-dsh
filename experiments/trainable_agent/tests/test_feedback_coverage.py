"""Feedback audits distinguish original mutations from semantic blind spots."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Literal

import pytest
from minidsh_rl import feedback_coverage
from minidsh_rl.feedback_coverage import CoverageRow, build_probes, summarize_rows
from minidsh_rl.feedback_fixtures import draft_feedback_tasks
from minidsh_rl.minirepo_tasks import MiniTask, development_tasks
from minidsh_rl.minirepo_verification import candidate_allowed
from minidsh_rl.verification import Verdict

from minidsh.infrastructure.process import ProcessRunner


def test_feedback_draft_changes_only_public_checks_and_identity() -> None:
    original = development_tasks()
    snapshot = tuple(t.files for t in original)
    drafts = draft_feedback_tasks(original)
    assert len(drafts) == len(original) == 8
    for before, after in zip(original, drafts, strict=True):
        assert after.case_id == before.case_id + "_feedback_v2"
        assert after.family == before.family
        assert after.specification == before.specification
        assert after.reference == before.reference
        assert after.hidden_checks == before.hidden_checks
        old, new = dict(before.files), dict(after.files)
        assert set(old) == set(new)
        assert [name for name in old if old[name] != new[name]] == ["public_tests.py"]
        assert new["public_tests.py"].count("print('public tests passed')") == 1
        assert new["public_tests.py"].endswith("print('public tests passed')\n")
        assert before.hidden_checks not in new["public_tests.py"]
    assert tuple(t.files for t in original) == snapshot


async def test_offline_audit_keeps_all_pairs_and_does_not_mutate_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = development_tasks()[:1]
    kinds = {p.candidate: p.kind for p in build_probes(tasks)}
    calls = []

    async def fake_verify(  # noqa: PLR0913 - independent verifier contract double
        host: ProcessRunner,
        task: MiniTask,
        candidate: dict[str, str],
        image: str,
        output: Path,
        *,
        checks: Literal["private", "public"] = "private",
    ) -> Verdict:
        del host, image
        kind = kinds[tuple(sorted(candidate.items()))]
        calls.append((kind, checks))
        passed = kind == "reference" or (kind == "semantic" and checks == "public")
        await asyncio.to_thread(output.mkdir, parents=True)
        (output / "result.json").write_text(
            json.dumps(
                {
                    "candidate_sha256": hashlib.sha256(
                        json.dumps(candidate, sort_keys=True).encode()
                    ).hexdigest(),
                    "passed": passed,
                    "case_id": task.case_id,
                }
            )
        )
        (output / "stdout.txt").write_text("")
        (output / "stderr.txt").write_text("")
        return Verdict(passed, 0 if passed else 1, timed_out=False, duration_ms=0.0)

    monkeypatch.setattr(feedback_coverage, "verify", fake_verify)
    ledger = tmp_path / "ledger.json"
    ledger.write_text('{"unchanged":true}')
    before = ledger.read_bytes()
    await feedback_coverage.audit(tasks, tmp_path / "audit", ledger)
    result = json.loads((tmp_path / "audit/summary.json").read_text())
    assert len(calls) == result["completed_checks"] == 12
    assert result["completed_probes"] == result["declared_probes"] == 6
    assert result["summary"]["reference_gate_passed"] is True
    assert result["summary"]["feedback_coverage_gate_passed"] is False
    assert result["summary"]["feedback_blind_spots"] == 1
    assert result["new_model_calls"] == result["new_training_calls"] == 0
    assert ledger.read_bytes() == before


def test_frozen_probe_set_has_all_tasks_categories_and_allowed_candidates() -> None:
    tasks = development_tasks()
    probes = build_probes(tasks)
    assert len(probes) == 48
    assert len({(p.case_id, p.probe_id) for p in probes}) == 48
    by_id = {t.case_id: t for t in tasks}
    for task in tasks:
        selected = [p for p in probes if p.case_id == task.case_id]
        assert len(selected) == 6
        assert sum(p.kind == "original_fault" for p in selected) == 2
        assert sum(p.kind == "semantic" for p in selected) == 1
    for probe in probes:
        assert candidate_allowed(by_id[probe.case_id], dict(probe.candidate))
        if probe.kind == "reference":
            assert probe.expected_correct is True
        else:
            assert probe.expected_correct is False
            assert probe.candidate != by_id[probe.case_id].reference


def test_unexecuted_or_timeout_probe_is_not_reported_as_functionally_detected() -> None:
    rows: list[CoverageRow] = [
        {
            "kind": "semantic",
            "expected_correct": False,
            "public": {"passed": True, "exit_code": 0, "timed_out": False},
            "private": {"passed": False, "exit_code": 1, "timed_out": False},
        },
        {
            "kind": "semantic",
            "expected_correct": False,
            "public": {"passed": False, "exit_code": -1, "timed_out": False},
            "private": {"passed": False, "exit_code": -1, "timed_out": False},
        },
        {
            "kind": "semantic",
            "expected_correct": False,
            "public": {"passed": False, "exit_code": 124, "timed_out": True},
            "private": {"passed": False, "exit_code": 1, "timed_out": False},
        },
    ]
    result = summarize_rows(rows)
    semantic = result["categories"]["semantic"]
    assert semantic["declared_incorrect"] == 3
    assert semantic["public_checked"] == 1
    assert semantic["public_detected"] == 0
    assert semantic["public_detection_rate_checked"] == 0.0
    assert result["feedback_blind_spots"] == 1
    assert result["feedback_coverage_gate_passed"] is False
    empty = summarize_rows([])
    assert empty["categories"] == {}
    assert empty["feedback_coverage_gate_passed"] is None
