"""The public acceptance CLI freezes explicit inputs without provider access."""

import asyncio
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

import pytest

from minidsh.adapters.memory import SQLiteMemoryStore
from minidsh.adapters.memory_evidence import JsonMemoryEvidenceSource
from minidsh.domain.memory import MemoryEntry, MemoryEvidenceFile, MemoryEvidenceGrant, MemoryKind

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "extension_acceptance.py"


def cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=ROOT,
        env={
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
            "MINIDSH_PROVIDER": "must_not_be_used",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


async def prepared_seed_fixture(tmp_path: Path) -> Path:
    """Synthetic approvals exercise the input contract, never real cohort approval."""
    fixture = runpy.run_path(str(ROOT / "tests/test_memory_composition.py"))
    workspace, config, environment = await fixture["_seed_configuration_memory"](tmp_path)
    evidence_root = Path(config.evidence_root)
    grants = list(config.evidence_grants)
    rows = [environment]
    store = SQLiteMemoryStore(
        Path(config.database), allowed_root=Path(config.storage_root), workspace=workspace
    )
    try:
        for index in range(2):
            prefix = f"repair_{index}"
            directory = evidence_root / prefix
            directory.mkdir()
            run_id = f"fixture_repair_{index}"
            facts = [
                {
                    "schema_version": "1.0",
                    "name": "run_started",
                    "run_id": run_id,
                    "step_id": 0,
                    "data": {},
                },
                {
                    "schema_version": "1.0",
                    "name": "run_finished",
                    "run_id": run_id,
                    "step_id": 1,
                    "data": {"status": "finished"},
                },
            ]
            (directory / "trace.jsonl").write_text(
                "".join(json.dumps(fact) + "\n" for fact in facts)
            )
            (directory / "criterion.py").write_text("assert True  # synthetic component proof\n")
            (directory / "artifact.txt").write_text("synthetic artifact\n")
            (directory / "acceptance.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "run_id": run_id,
                        "case_id": prefix,
                        "source": prefix + "/criterion.py",
                        "task_success": True,
                    }
                )
            )
            references = {
                name: {
                    "path": prefix + "/" + name,
                    "sha256": sha256((directory / name).read_bytes()).hexdigest(),
                }
                for name in ("trace.jsonl", "criterion.py", "artifact.txt", "acceptance.json")
            }
            (directory / "bundle.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "kind": "independent_acceptance",
                        "project_id": config.project_id,
                        "applicable_version": config.applicable_version,
                        "source_run_id": run_id,
                        "acceptance": references["acceptance.json"],
                        "trace": references["trace.jsonl"],
                        "criterion": references["criterion.py"],
                        "artifacts": [references["artifact.txt"]],
                    }
                )
            )
            grant = MemoryEvidenceGrant(
                prefix,
                MemoryEvidenceFile(
                    prefix + "/bundle.json",
                    sha256((directory / "bundle.json").read_bytes()).hexdigest(),
                ),
            )
            grants.append(grant)
            reader = JsonMemoryEvidenceSource(
                evidence_root, workspace=workspace, grants=tuple(grants)
            )
            proposal = MemoryEntry.propose(
                prefix,
                config.project_id,
                MemoryKind.REPAIR,
                topic=prefix,
                content=f"Synthetic reusable diagnosis {index}.",
                source_run_id=run_id,
                applicable_version=config.applicable_version,
            )
            validated = proposal.validate(await reader.resolve(prefix))
            approved = validated.approve(
                expected_revision=1,
                content_sha256=validated.content_sha256,
                approver="fixture_operator",
                approved_at="2026-10-29T00:00:00+00:00",
            )
            committed = approved.commit(
                expected_revision=1, approval_sha256=approved.approval_sha256
            )
            await store.create(proposal)
            for before, after in (
                (proposal, validated),
                (validated, approved),
                (approved, committed),
            ):
                await store.replace(before, after)
            rows.append(committed)
    finally:
        await store.close()
    bindings = [
        {"entry_id": row.entry_id, "revision": row.revision, "content_sha256": row.content_sha256}
        for row in rows
    ]
    approval = tmp_path / "approval.json"
    approval.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "purpose": "extension_v1_frozen_queue",
                "project_id": config.project_id,
                "applicable_version": config.applicable_version,
                "approved_by": "fixture_operator",
                "approved_at": "2026-10-29T00:00:00+00:00",
                "review_sha256": "a" * 64,
                "entries": bindings,
            }
        )
    )
    raw = asdict(config)
    raw["evidence_grants"] = [asdict(grant) for grant in grants]
    path = tmp_path / "seed.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "memory": raw,
                "database_sha256": sha256(
                    await asyncio.to_thread(Path(config.database).read_bytes)
                ).hexdigest(),
                "approval": {
                    "path": str(approval),
                    "sha256": sha256(approval.read_bytes()).hexdigest(),
                },
                "entries": bindings,
            }
        )
    )
    return path


def test_freeze_check_detects_tampering_and_never_overwrites_evidence(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    frozen = cli("freeze", "--output", str(snapshot))
    assert frozen.returncode == 0, frozen.stderr
    manifest = json.loads((snapshot / "manifest.json").read_text())
    assert manifest["protocol"]["real_model_runs"] == 90
    assert len(manifest["protocol"]["queue"]) == 90
    assert not any(".env" in name for name in manifest["source_files"])
    assert cli("check", "--snapshot", str(snapshot)).returncode == 0
    original = (snapshot / "manifest.json").read_bytes()
    assert cli("freeze", "--output", str(snapshot)).returncode != 0
    assert (snapshot / "manifest.json").read_bytes() == original
    archive = snapshot / "source.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"tampered")
    assert cli("check", "--snapshot", str(snapshot)).returncode != 0


def test_report_keeps_all_unrun_rows_unknown_and_uses_a_new_directory(tmp_path: Path) -> None:
    snapshot, output = tmp_path / "snapshot", tmp_path / "report"
    assert cli("freeze", "--output", str(snapshot)).returncode == 0
    derived = cli("report", "--snapshot", str(snapshot), "--output", str(output))
    assert derived.returncode == 0, derived.stderr
    report = json.loads((output / "report.json").read_text())
    assert report["derived"] is True
    assert report["declared_runs"] == 90
    assert report["executed_runs"] == 0
    assert len(report["results"]) == 90
    assert all(row["status"] == "not_run" for row in report["results"])
    assert report["task_success_rate"] == {
        "numerator": 0,
        "denominator": 0,
        "eligible": 90,
        "coverage": 0.0,
        "value": None,
    }
    assert report["total_tokens"] is None
    assert report["token_usage_complete"] is False
    assert report["known_tokens"] == 0
    assert all(
        row["status"] == "untested" for row in json.loads((output / "coverage.json").read_text())
    )
    assert cli("report", "--snapshot", str(snapshot), "--output", str(output)).returncode != 0


@pytest.mark.parametrize("stage", ["pr4", "m5"])
def test_published_derived_report_does_not_change_the_frozen_source_identity(
    tmp_path: Path, stage: str
) -> None:
    snapshot = tmp_path / "snapshot"
    assert cli("freeze", "--output", str(snapshot)).returncode == 0
    report = ROOT / "docs/results" / f"extension-{stage}-generated-contract.json"
    try:
        with report.open("x") as stream:
            json.dump({"scope": "derived output, never an execution input"}, stream)
        checked = cli("check", "--snapshot", str(snapshot))
        assert checked.returncode == 0, checked.stderr
    finally:
        report.unlink(missing_ok=True)


def test_default_run_is_the_twelve_frozen_offline_compatibility_runs(tmp_path: Path) -> None:
    snapshot, evidence, output = (tmp_path / name for name in ("snapshot", "evidence", "report"))
    assert cli("freeze", "--output", str(snapshot)).returncode == 0
    executed = cli("run", "--snapshot", str(snapshot), "--output", str(evidence))
    assert executed.returncode == 0, executed.stderr
    execution = json.loads((evidence / "execution.json").read_text())
    assert execution["provider"] == "scripted"
    assert execution["real_model_api_calls"] == 0
    assert execution["attempted_rows"] == 12
    assert execution["compatibility"]["all_equal"] is True
    assert len(execution["compatibility"]["cases"]) == 6
    assert cli("run", "--snapshot", str(snapshot), "--output", str(evidence)).returncode != 0
    derived = cli(
        "report",
        "--snapshot",
        str(snapshot),
        "--evidence",
        str(evidence),
        "--output",
        str(output),
    )
    assert derived.returncode == 0, derived.stderr
    report = json.loads((output / "report.json").read_text())
    assert report["executed_runs"] == 12
    assert report["task_success_rate"]["numerator"] == 12
    assert report["task_success_rate"]["denominator"] == 12
    assert report["task_success_rate"]["eligible"] == 90
    assert report["token_usage_complete"] is False
    assert report["real_model_api_calls"] == 0
    assert report["paired_comparison"]["declared_runs"] == 90
    assert report["paired_comparison"]["complete_cohort"] is False
    comparison = report["paired_comparison"]["comparisons"]["A_minus_Baseline"]
    assert comparison["known_outcome_pairs"] == 6
    assert comparison["declared_pairs"] == 18
    assert comparison["metrics"]["success_rate"]["delta"] is None
    assert comparison["benefit"] == "not_assessable"
    assert len(list((evidence / "runs").glob("*/trace.jsonl"))) == 12
    refused = cli(
        "run",
        "--snapshot",
        str(snapshot),
        "--output",
        str(tmp_path / "real"),
        "--provider",
        "deepseek",
    )
    assert refused.returncode != 0
    assert "approved_frozen_seeds" in refused.stderr
    assert not (tmp_path / "real").exists()


def test_report_retains_failed_unknown_and_unfinished_consumption(tmp_path: Path) -> None:
    snapshot, evidence, output = (tmp_path / name for name in ("snapshot", "evidence", "report"))
    assert cli("freeze", "--output", str(snapshot)).returncode == 0
    frozen = json.loads((snapshot / "manifest.json").read_text())
    evidence.mkdir()
    (evidence / "execution.json").write_text(
        json.dumps(
            {
                "source_sha256": frozen["source_sha256"],
                "snapshot_manifest_sha256": sha256(
                    (snapshot / "manifest.json").read_bytes()
                ).hexdigest(),
                "real_model_api_calls": 0,
                "mode": "synthetic_e03_contract",
            }
        )
    )
    budget_source = b'{"budget":{"max_tokens":8000}}\n'
    for index in range(4):
        row = frozen["protocol"]["queue"][index]
        directory = (
            evidence / "runs" / f"{index:03d}-{row['case_id']}-{row['repeat']}-{row['group']}"
        )
        directory.mkdir(parents=True)
        if index == 3:
            (directory / "configuration.json").write_bytes(budget_source)
        events: list[tuple[str, dict[str, object]]] = [("model_call_started", {})]
        if index == 1:
            events.append(("model_call_failed", {"usage_known": False}))
        else:
            events.append(
                (
                    "model_call_completed",
                    {
                        "input_tokens": 100 if index == 0 else 30 if index == 2 else 9000,
                        "output_tokens": 20 if index == 0 else 10 if index == 2 else 0,
                        "cached_input_tokens": 0,
                    },
                )
            )
        if index < 2:
            events.append(
                (
                    "run_finished",
                    {
                        "status": "budget_exhausted" if index == 0 else "failed",
                        "steps": 1,
                        "tool_calls": 0,
                        "duration_ms": 2,
                    },
                )
            )
            (directory / "evidence.json").write_text(
                json.dumps(
                    {
                        "run_id": f"fixture_{index}",
                        "case_id": row["case_id"],
                        "source": "synthetic_e03_contract",
                        "task_success": index == 0,
                        "budget": {"max_tokens": 100},
                    }
                )
            )
        (directory / "trace.jsonl").write_text(
            "".join(
                json.dumps(
                    {
                        "name": name,
                        "run_id": f"fixture_{index}",
                        "step_id": 1,
                        "data": data,
                        "schema_version": "1.0",
                        "occurred_at": "2026-10-29T00:00:00+00:00",
                    }
                )
                + "\n"
                for name, data in events
            )
        )
    derived = cli(
        "report",
        "--snapshot",
        str(snapshot),
        "--evidence",
        str(evidence),
        "--output",
        str(output),
    )
    assert derived.returncode == 0, derived.stderr
    report = json.loads((output / "report.json").read_text())
    assert report["declared_runs"] == 90
    assert report["executed_runs"] == 4
    assert report["unobserved_rows"] == 86
    assert report["known_tokens"] == 9160
    assert report["total_tokens"] is None
    assert report["token_usage_complete"] is False
    assert report["known_unknown_usage_calls"] == 1
    assert report["tokens_per_success"] is None
    assert report["known_tokens_per_success"] == 9160
    assert report["task_success_rate"]["numerator"] == 1
    assert report["task_success_rate"]["denominator"] == 2
    assert report["task_success_rate"]["eligible"] == 90
    first, _unknown, unfinished, partial_overrun = report["results"][:4]
    assert first["score"]["measurements"]["token_overrun"] == 20
    assert first["score"]["rates"]["constraint_satisfied_run_rate"]["value"] == 0
    assert unfinished["score"]["measurements"]["finalized"] is False
    assert partial_overrun["score"]["measurements"]["token_overrun"] == 1000
    assert (
        partial_overrun["inputs_sha256"]["configuration.json"] == sha256(budget_source).hexdigest()
    )
    assert (
        report["execution_sha256"] == sha256((evidence / "execution.json").read_bytes()).hexdigest()
    )
    assert partial_overrun["score"]["rates"]["constraint_satisfied_run_rate"]["value"] == 0


def test_check_validates_seed_rows_approval_binding_and_real_evidence_bytes(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    assert cli("freeze", "--output", str(snapshot)).returncode == 0
    seed = asyncio.run(prepared_seed_fixture(tmp_path))
    checked = cli("check", "--snapshot", str(snapshot), "--seed-manifest", str(seed))
    assert checked.returncode == 0, checked.stderr
    raw = json.loads(seed.read_text())
    database = Path(raw["memory"]["database"])
    with database.open("ab") as stream:
        stream.write(b"changed seed bytes")
    refused = cli("check", "--snapshot", str(snapshot), "--seed-manifest", str(seed))
    assert refused.returncode != 0
    assert "memory_seed_changed" in refused.stderr
    assert str(database) not in refused.stderr


def test_seed_freeze_preserves_bytes_for_historical_audit_after_source_changes(
    tmp_path: Path,
) -> None:
    seed = asyncio.run(prepared_seed_fixture(tmp_path))
    snapshot = tmp_path / "snapshot"
    frozen = cli("freeze", "--output", str(snapshot), "--seed-manifest", str(seed))
    assert frozen.returncode == 0, frozen.stderr
    manifest = json.loads((snapshot / "manifest.json").read_text())
    assert manifest["memory_seed"]["manifest_sha256"] == sha256(seed.read_bytes()).hexdigest()
    assert (snapshot / "memory_seed.sqlite3").read_bytes() == Path(
        json.loads(seed.read_text())["memory"]["database"]
    ).read_bytes()
    assert cli("check", "--snapshot", str(snapshot)).returncode == 0
    offline = cli("run", "--snapshot", str(snapshot), "--output", str(tmp_path / "offline"))
    assert offline.returncode == 0, offline.stderr
    original = (snapshot / "memory_seed.sqlite3").read_bytes()
    source = Path(json.loads(seed.read_text())["approval"]["path"])
    source.write_text("changed approval source")
    assert cli("check", "--snapshot", str(snapshot)).returncode != 0
    assert cli("check", "--snapshot", str(snapshot), "--archive-only").returncode == 0
    assert (snapshot / "memory_seed.sqlite3").read_bytes() == original
    assert list((snapshot / "memory_evidence").rglob("bundle.json"))


def test_phase_gate_requires_all_cases_and_unchanged_indexed_evidence(tmp_path: Path) -> None:
    snapshot, phase = tmp_path / "snapshot", tmp_path / "phase"
    seed = asyncio.run(prepared_seed_fixture(tmp_path))
    assert cli("freeze", "--output", str(snapshot), "--seed-manifest", str(seed)).returncode == 0
    frozen = json.loads((snapshot / "manifest.json").read_text())
    phase.mkdir()
    rows, files = [], {}
    for case in frozen["protocol"]["cases"]:
        name = case["case_id"] + ".json"
        path = phase / name
        path.write_text(json.dumps({"scope": "synthetic_gate_contract", "status": "pass"}))
        files[name] = sha256(path.read_bytes()).hexdigest()
        rows.append({"case_id": case["case_id"], "status": "pass", "evidence": name})
    (phase / "coverage.json").write_text(
        json.dumps({"source_sha256": frozen["source_sha256"], "rows": rows})
    )
    files["coverage.json"] = sha256((phase / "coverage.json").read_bytes()).hexdigest()
    (phase / "evidence_index.json").write_text(json.dumps({"files": files}))
    seal = phase / "seal.json"
    seal.write_text(
        json.dumps(
            {
                "status": "accepted_pr4_only",
                "source_sha256": frozen["source_sha256"],
                "memory_seed_manifest_sha256": frozen["memory_seed"]["manifest_sha256"],
                "evidence_index_sha256": sha256(
                    (phase / "evidence_index.json").read_bytes()
                ).hexdigest(),
            }
        )
    )
    assert cli("check", "--snapshot", str(snapshot), "--pr4-acceptance", str(seal)).returncode == 0
    real_output = tmp_path / "real_output"
    synthetic = cli(
        "run",
        "--snapshot",
        str(snapshot),
        "--provider",
        "deepseek",
        "--pr4-acceptance",
        str(seal),
        "--output",
        str(real_output),
    )
    assert synthetic.returncode != 0
    assert "human_seed_approval_required" in synthetic.stderr
    assert not real_output.exists()
    derived = tmp_path / "accepted_safety_report"
    audited = cli(
        "report",
        "--snapshot",
        str(snapshot),
        "--output",
        str(derived),
        "--pr4-acceptance",
        str(seal),
    )
    assert audited.returncode == 0, audited.stderr
    report = json.loads((derived / "report.json").read_text())
    assert report["accepted_safety_receipt"]["sha256"] == sha256(seal.read_bytes()).hexdigest()
    assert report["paired_comparison"]["safety_passed"] is True
    assert report["paired_comparison"]["complete_cohort"] is False
    (phase / rows[0]["evidence"]).write_text("changed evidence")
    refused = cli("check", "--snapshot", str(snapshot), "--pr4-acceptance", str(seal))
    assert refused.returncode != 0
    assert "pr4_acceptance_evidence_changed" in refused.stderr
    refused_report = tmp_path / "tampered_safety_report"
    refused = cli(
        "report",
        "--snapshot",
        str(snapshot),
        "--output",
        str(refused_report),
        "--pr4-acceptance",
        str(seal),
    )
    assert refused.returncode != 0
    assert not refused_report.exists()


def test_isolated_matrix_requires_frozen_seed_before_process_or_provider_access(
    tmp_path: Path,
) -> None:
    snapshot, output = tmp_path / "snapshot", tmp_path / "matrix"
    assert cli("freeze", "--output", str(snapshot)).returncode == 0
    refused = cli("run", "--snapshot", str(snapshot), "--output", str(output), "--isolated-matrix")
    assert refused.returncode != 0
    assert "approved_frozen_seeds_required" in refused.stderr
    assert not output.exists()


def test_candidate_inspection_preserves_binary_and_never_follows_host_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    outside = tmp_path / "host_private.txt"
    outside.write_text("host_private_canary")
    archive = tmp_path / "candidate.tar"
    raw = b"source\x00\xffbytes"
    with tarfile.open(archive, "w") as stream:
        regular = tarfile.TarInfo("solution.js")
        regular.size = len(raw)
        stream.addfile(regular, io.BytesIO(raw))
        link = tarfile.TarInfo("external_alias")
        link.type, link.linkname = tarfile.SYMTYPE, str(outside)
        stream.addfile(link)
    api = runpy.run_path(str(SCRIPT), run_name="candidate_inspection_contract")
    outside.chmod(0o000)
    try:
        inspected = api["archive_hashes"](archive)
    finally:
        outside.chmod(0o600)
    assert inspected == {
        "solution.js": {"kind": "file", "sha256": sha256(raw).hexdigest()},
        "external_alias": {"kind": "symlink", "target": str(outside)},
    }
    assert outside.read_text() == "host_private_canary"
    assert not (tmp_path / "solution.js").exists()
    assert not (tmp_path / "external_alias").exists()


def test_protocol_check_rejects_unlisted_fixture_files_before_they_can_be_copied(
    tmp_path: Path,
) -> None:
    temporary = tmp_path / "source"
    shutil.copytree(ROOT / "benchmarks/extension_v1", temporary / "benchmarks/extension_v1")
    shutil.copytree(ROOT / "examples/skills", temporary / "examples/skills")
    protocol = json.loads((temporary / "benchmarks/extension_v1/manifest.json").read_text())
    api = runpy.run_path(str(SCRIPT), run_name="frozen_input_contract")
    api["check_protocol"](protocol, temporary)
    injected = temporary / "benchmarks/extension_v1/test_failure/workspace/injected.bin"
    injected.write_bytes(b"unlisted private material")
    with pytest.raises(RuntimeError, match="frozen_task_tree_changed"):
        api["check_protocol"](protocol, temporary)


@pytest.mark.parametrize("model_called", [False, True])
def test_report_distinguishes_memory_selection_from_actual_model_exposure(
    tmp_path: Path, *, model_called: bool
) -> None:
    snapshot, evidence, output = (tmp_path / name for name in ("snapshot", "evidence", "report"))
    assert cli("freeze", "--output", str(snapshot)).returncode == 0
    frozen = json.loads((snapshot / "manifest.json").read_text())
    evidence.mkdir()
    (evidence / "execution.json").write_text(
        json.dumps(
            {
                "source_sha256": frozen["source_sha256"],
                "snapshot_manifest_sha256": sha256(
                    (snapshot / "manifest.json").read_bytes()
                ).hexdigest(),
                "real_model_api_calls": 0,
                "provider": "scripted",
                "mode": "synthetic_memory_diagnostic",
                "status": "interrupted",
            }
        )
    )
    index, declared = next(
        (i, row) for i, row in enumerate(frozen["protocol"]["queue"]) if row["group"] == "D"
    )
    folder = evidence / "runs" / f"{index:03d}-{declared['case_id']}-{declared['repeat']}-D"
    folder.mkdir(parents=True)
    events: list[tuple[str, dict[str, object]]] = [
        ("run_started", {}),
        ("context_built", {"metadata": {"memory_match_count": 1}}),
    ]
    if model_called:
        events.extend(
            [
                ("model_call_started", {"messages": [{"role": "user", "content": "fixture task"}]}),
                ("model_call_completed", {"input_tokens": 10, "output_tokens": 1}),
            ]
        )
    events.append(("run_finished", {"status": "finished", "steps": 1, "duration_ms": 3}))
    (folder / "trace.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "name": name,
                    "run_id": "diagnostic_run",
                    "step_id": 1,
                    "data": data,
                }
            )
            + "\n"
            for name, data in events
        )
    )
    result = cli(
        "report", "--snapshot", str(snapshot), "--evidence", str(evidence), "--output", str(output)
    )
    assert result.returncode == 0, result.stderr
    group = json.loads((output / "report.json").read_text())["paired_comparison"]["groups"]["D"]
    assert group["memory_selector_match"]["numerator"] == 1
    assert group["memory_selector_match"]["denominator"] == 1
    assert group["memory_selector_match"]["eligible"] == 18
    assert group["memory_model_exposure"]["numerator"] == 0
    assert group["memory_model_exposure"]["denominator"] == int(model_called)
    assert group["memory_model_exposure"]["value"] == (0 if model_called else None)
