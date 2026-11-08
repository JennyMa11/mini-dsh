"""Offline data gating binds new versions without relabelling old verifier records."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Literal

import pytest
from minidsh_rl import learning_gates
from minidsh_rl.learning_tasks import learning_cases
from minidsh_rl.minirepo_tasks import MiniTask
from minidsh_rl.verification import Verdict

from minidsh.infrastructure.process import ProcessRunner


async def test_learning_audit_binds_criteria_and_all_checks_without_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = learning_cases()[0]
    calls: list[str] = []

    async def verify(  # noqa: PLR0913 - exact existing verification port
        host: ProcessRunner,
        task: MiniTask,
        candidate: dict[str, str],
        image: str,
        output: Path,
        *,
        checks: Literal["public", "private"] = "private",
    ) -> Verdict:
        del host, image
        calls.append(checks)
        passed = candidate == dict(task.reference)
        await asyncio.to_thread(output.mkdir, parents=True)
        (output / "result.json").write_text(
            json.dumps(
                {
                    "case_id": task.case_id,
                    "criterion_version": "minirepo_dev_v1",
                    "candidate_sha256": hashlib.sha256(
                        json.dumps(candidate, sort_keys=True).encode()
                    ).hexdigest(),
                    "passed": passed,
                }
            )
        )
        (output / "stdout.txt").write_text("")
        (output / "stderr.txt").write_text("")
        return Verdict(passed, 0 if passed else 1, timed_out=False, duration_ms=0.0)

    monkeypatch.setattr(learning_gates, "verify", verify)
    ledger = tmp_path / "ledger.json"
    ledger.write_text('{"fixed":true}')
    before = ledger.read_bytes()
    output = tmp_path / "audit"
    await learning_gates.audit((case,), output, ledger)
    summary = json.loads((output / "summary.json").read_text())
    assert len(calls) == summary["completed_checks"] == 14
    assert summary["completed_probes"] == summary["declared_probes"] == 7
    assert summary["coverage"]["reference_gate_passed"] is True
    assert summary["coverage"]["feedback_coverage_gate_passed"] is True
    assert summary["complete_dataset"] is False
    assert summary["new_model_calls"] == summary["new_training_calls"] == 0
    assert ledger.read_bytes() == before
    assert (
        summary["case_bindings"][case.task.case_id]["criterion_version"] == case.criterion_version
    )
    sidecars = list(output.glob("*__*/private/criterion-binding.json"))
    assert len(sidecars) == 7
    sidecar = json.loads(sidecars[0].read_text())
    assert sidecar["criterion_version"] == "learnability_private_v1"
    assert sidecar["source_verifier_criterion_version"] == "minirepo_dev_v1"
    assert sidecar["checker_sha256"] == hashlib.sha256(case.task.hidden_checks.encode()).hexdigest()
