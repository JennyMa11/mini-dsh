"""P5 freezes inputs, keeps proposals unapproved and rejects synthetic real queues."""

import io
import json
import runpy
import tarfile
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from minidsh.adapters import JsonlTraceWriter, ScriptedModel
from minidsh.benchmark.memory_study import MemoryArm, memory_study_options, study_protocol
from minidsh.composition import RuntimeOptions, build_local_runtime
from minidsh.domain import ModelCallError, ModelRequest, ModelResponse, TokenUsage, ToolCall
from minidsh.evaluation.extension_comparison import ComparisonRun

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


def _module() -> SimpleNamespace:
    return SimpleNamespace(**runpy.run_path(str(ROOT / "scripts/memory_acceptance.py")))


@pytest.mark.parametrize("change", ["queue", "budget", "model", "bool_model", "extra"])
def test_p501_protocol_rejects_changed_opportunities_or_conditions(
    tmp_path: Path, change: str
) -> None:
    protocol = study_protocol()
    if change == "queue":
        protocol["queue"][-1] = protocol["queue"][0]
    elif change == "budget":
        protocol["budget"]["max_tokens"] += 1
    elif change == "model":
        protocol["model"]["endpoint"] = "https://invalid.example/completions"
    elif change == "bool_model":
        protocol["model"]["temperature"] = False
    else:
        protocol["accepted"] = True
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol))
    with pytest.raises(ValueError, match=r"memory_study_|Extra inputs"):
        study_protocol(path)


def test_p501_source_package_roundtrip_and_archive_tamper_is_rejected(tmp_path: Path) -> None:
    mod = _module()
    snapshot = tmp_path / "snapshot"
    frozen = mod.plan(snapshot)
    assert mod.check(snapshot)["source_sha256"] == frozen["source_sha256"]
    with (snapshot / "source.tar.gz").open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(mod.StudyError, match="source_archive_changed"):
        mod.check(snapshot)


def test_p502_options_can_be_recorded_without_changing_permissions(tmp_path: Path) -> None:
    mod = _module()
    options = memory_study_options(MemoryArm.OFF, None)
    path = tmp_path / "options.json"
    mod.save(path, asdict(options))
    saved = json.loads(path.read_text())
    assert set(saved["enabled_tools"]) == {"read_file", "write_file", "patch_file", "test"}
    assert saved["memory"] is None


async def _prepared(root: Path, mod: Any) -> tuple[Path, Path]:
    fixtures = runpy.run_path(str(ROOT / "tests/test_extension_acceptance.py"))
    source_root = root / "source"
    source_root.mkdir()
    source = await fixtures["prepared_seed_fixture"](source_root)
    snapshot = root / "snapshot"
    mod.plan(snapshot)
    prepared = root / "prepared"
    await mod.prepare(snapshot, source, prepared, provider="scripted", credentials=None)
    return snapshot, prepared


@pytest.mark.asyncio
async def test_p503_505_proposal_review_then_fixture_commit_cannot_authorize_real_queue(
    tmp_path: Path,
) -> None:
    mod = _module()
    snapshot, prepared = await _prepared(tmp_path, mod)
    review = mod.read(prepared / "review.json")
    assert review["selected"]["status"] == "proposed"
    assert review["selected"]["approval_sha256"] is None
    assert len(review["selected"]["content"].encode()) <= 2048
    authority = mod.read(prepared / "authorization-template.json")
    authority.update(
        authorization_kind="synthetic_fixture",
        approved_by="fixture_operator",
        approved_at="2026-11-12T00:00:00+00:00",
    )
    authority_file = tmp_path / "authority.json"
    mod.save(authority_file, authority)
    committed = tmp_path / "committed"
    seal = await mod.authorize(snapshot, prepared, authority_file, committed, fixture=True)
    assert seal["authorization_kind"] == "synthetic_fixture"
    _, _, configs = await mod.checked_seed(snapshot, committed, real=False)
    assert configs["original"].project_id == configs["consolidated"].project_id
    with pytest.raises(mod.StudyError, match="explicit_user_seed_approval_required"):
        await mod.run(snapshot, committed, tmp_path / "real_execution", provider="deepseek")
    assert not (tmp_path / "real_execution").exists()
    assert not (committed / "real_queue_reserved.json").exists()
    assert mod.read(prepared / "review.json")["selected"]["status"] == "proposed"


@pytest.mark.asyncio
async def test_p503_review_hash_must_match_exact_authority_before_clone(tmp_path: Path) -> None:
    mod = _module()
    snapshot, prepared = await _prepared(tmp_path, mod)
    authority = mod.read(prepared / "authorization-template.json")
    authority.update(
        authorization_kind="synthetic_fixture",
        approved_by="fixture_operator",
        approved_at="2026-11-12T00:00:00+00:00",
        review_sha256="0" * 64,
    )
    path = tmp_path / "authority.json"
    mod.save(path, authority)
    with pytest.raises(mod.StudyError, match="explicit_seed_authorization_mismatch"):
        await mod.authorize(snapshot, prepared, path, tmp_path / "committed", fixture=True)
    assert not (tmp_path / "committed").exists()


@pytest.mark.asyncio
async def test_p507_failed_model_reported_usage_survives_missing_trace(tmp_path: Path) -> None:
    mod = _module()

    class Failed(ScriptedModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            raise ModelCallError("fixed failure", usage=TokenUsage(31, 7))

    model = mod.UsageObservedModel(Failed([]))
    with pytest.raises(ModelCallError):
        await model.complete(ModelRequest((), ()))
    values = mod.usage_metrics(tmp_path, model)
    assert values["total_tokens"] == 38
    assert values["token_usage_complete"] is False
    assert values["finalized"] is False


@pytest.mark.asyncio
async def test_p507_unknown_response_usage_remains_unknown(tmp_path: Path) -> None:
    mod = _module()
    model = mod.UsageObservedModel(ScriptedModel([ModelResponse("unknown", usage_known=False)]))
    await model.complete(ModelRequest((), ()))
    values = mod.usage_metrics(tmp_path, model)
    assert values["unknown_usage_calls"] == 1
    assert values["token_usage_complete"] is False


def test_p506_credential_audit_detects_plain_and_compressed_leaks_without_echo(
    tmp_path: Path,
) -> None:
    mod = _module()
    canary = "p5-offline-canary-credential"
    (tmp_path / "trace.jsonl").write_text(canary)
    payload = f"before {canary} after".encode()
    with tarfile.open(tmp_path / "source.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo("src/fixture.py")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    found = mod.secret_audit(tmp_path, canary)
    assert found["matched_units"] >= 2
    assert found["checked_archive_members"] == 1
    assert canary not in json.dumps(found)
    (tmp_path / "trace.jsonl").write_text("redacted")
    with tarfile.open(tmp_path / "source.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo("src/fixture.py")
        member.size = len(b"redacted")
        archive.addfile(member, io.BytesIO(b"redacted"))
    assert mod.secret_audit(tmp_path, canary)["matched_units"] == 0


def test_p506_audit_rejects_symlink_instead_of_reading_outside_inputs(tmp_path: Path) -> None:
    mod = _module()
    private = tmp_path / "private"
    private.write_text("canary")
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "link").symlink_to(private)
    with pytest.raises(mod.StudyError, match="audit_symlink_denied"):
        mod.secret_audit(inputs, "canary")


def test_p506_usage_witness_keeps_credential_input_audit_before_model_calls(
    tmp_path: Path,
) -> None:
    mod = _module()
    material = tmp_path / "material.json"
    material.write_text("offline-private-canary")
    inner = ScriptedModel([])
    model = mod.UsageObservedModel(mod.AuditedCredentialModel(inner, "offline-private-canary"))
    result = model.audit(tmp_path)
    assert result["matched_units"] == 1
    assert inner.requests == []
    assert "offline-private-canary" not in json.dumps(result)


@pytest.mark.asyncio
async def test_p507_interrupted_slot_retains_failure_cost_and_unknown_safety(
    tmp_path: Path,
) -> None:
    mod = _module()

    class Failed(ScriptedModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            raise ModelCallError("fixed failure", usage=TokenUsage(31, 7))

    model = mod.UsageObservedModel(Failed([]))
    with pytest.raises(ModelCallError):
        await model.complete(ModelRequest((), ()))
    directory = tmp_path / "runs/slot"
    directory.mkdir(parents=True)
    state: dict[str, Any] = {
        "checked_slots": 0,
        "rows": [],
        "preparation_tokens": 17,
        "safety_passed": None,
    }
    slot = {"case_id": "case", "repeat": 0, "group": "Baseline"}
    mod.record_partial(state, (0, slot, directory, model), tmp_path)
    assert state["known_tokens"] == 55
    assert state["checked_slots"] == 0
    assert state["safety_passed"] is None
    assert state["rows"][0]["usage_complete"] is False
    assert state["rows"][0]["finalized"] is False
    # Re-recording the same interruption cannot double-count failed consumption.
    mod.record_partial(state, (0, slot, directory, model), tmp_path)
    assert state["known_tokens"] == 55
    assert len(state["rows"]) == 1


def test_p507_all_arms_report_exposure_coverage_against_declared_opportunities() -> None:
    mod = _module()
    cases = [f"case_{i}" for i in range(6)]
    measured = ComparisonRun(
        cases[0], 0, "A", memory_exposed=True, tokens=7, usage_complete=True, finalized=True
    )
    report = mod.study_comparison([measured], cases, safety_passed=None, real=False).to_json()
    a = report["groups"]["A"]["memory_model_exposure"]
    assert a == {
        "numerator": 1,
        "denominator": 1,
        "eligible": 18,
        "coverage": 1 / 18,
        "value": 1.0,
    }
    for group in ("Baseline", "B", "C", "D"):
        exposure = report["groups"][group]["memory_model_exposure"]
        assert exposure["eligible"] == 18
        assert exposure["denominator"] == 0
        assert exposure["coverage"] == 0.0
        assert exposure["value"] is None
    assert report["groups"]["A"]["memory_selector_match"]["coverage"] is None


def _native_receipts(mod: Any, frozen: dict[str, Any], preflight: Path) -> dict[str, str]:
    """Metadata fixtures for validator tests; not proof of native execution."""
    probes = mod.probe_module()
    for name, cases in (("mechanisms", probes.MECHANISM_CASES), ("os", probes.OS_CASES)):
        directory = preflight / "native" / name
        directory.mkdir(parents=True)
        criteria = {
            "source_sha256": frozen["source_sha256"],
            "protocol_sha256": frozen["protocol_sha256"],
            "driver_sha256": frozen["source_files"]["scripts/memory_study_probes.py"],
            "cases": sorted(cases),
        }
        mod.save(directory / "criteria.json", criteria)
        result = {
            **criteria,
            "checks": {case: {"predicate": True} for case in cases},
            "criteria_sha256": mod.pin(directory / "criteria.json"),
            "passed": True,
            "real_model_calls": 0,
        }
        mod.save(directory / "checks.json", result)
    return cast("dict[str, str]", mod.check_native_probes(frozen, preflight))


def _preflight_fixture(mod: Any, snapshot: Path, preflight: Path) -> None:
    preflight.mkdir()
    frozen = mod.check(snapshot)
    hashes = _native_receipts(mod, frozen, preflight)
    mod.save(
        preflight / "contracts.json",
        {
            "source_sha256": frozen["source_sha256"],
            "protocol_sha256": frozen["protocol_sha256"],
            "passed": True,
            "rows": [{"status": "passed"}],
        },
    )
    (preflight / "reconstruction.log").write_text("metadata fixture, not a native run")
    mod.save(
        preflight / "reconstruction.json",
        {
            "source_sha256": frozen["source_sha256"],
            "source_archive_sha256": frozen["source_archive_sha256"],
            "source_files": len(frozen["source_files"]),
            "exit_code": 0,
            "timed_out": False,
            "log_sha256": mod.pin(preflight / "reconstruction.log"),
        },
    )
    mod.save(
        preflight / "preflight.json",
        {
            "source_sha256": frozen["source_sha256"],
            "protocol_sha256": frozen["protocol_sha256"],
            "passed": True,
            "offline_checked_slots": 90,
            "native_sha256": hashes,
            "contracts_sha256": mod.pin(preflight / "contracts.json"),
            "reconstruction_sha256": mod.pin(preflight / "reconstruction.json"),
            "files": mod.files_in(preflight),
        },
    )


@pytest.mark.parametrize("change", ["missing", "failed", "source", "case", "non_boolean"])
def test_p504_green_offline_cohort_cannot_replace_native_safety_evidence(
    tmp_path: Path, change: str
) -> None:
    mod = _module()
    snapshot = tmp_path / "snapshot"
    frozen = mod.plan(snapshot)
    preflight = tmp_path / "preflight"
    preflight.mkdir()
    hashes = _native_receipts(mod, frozen, preflight)
    target = preflight / "native/os/checks.json"
    value = mod.read(target)
    if change == "missing":
        target.unlink()
    else:
        if change == "failed":
            value["checks"]["cancel"]["predicate"] = False
        elif change == "source":
            value["source_sha256"] = "0" * 64
        elif change == "case":
            value["checks"].pop("cancel")
        else:
            value["checks"]["cancel"]["predicate"] = 1
        mod.save(target, value)
    mod.save(
        preflight / "preflight.json",
        {
            "source_sha256": frozen["source_sha256"],
            "protocol_sha256": frozen["protocol_sha256"],
            "passed": True,
            "offline_checked_slots": 90,
            "native_sha256": hashes,
            "files": mod.files_in(preflight),
        },
    )
    with pytest.raises(mod.StudyError, match="preflight_native_"):
        mod.check_preflight(snapshot, preflight)


@pytest.mark.asyncio
async def test_p504_preparation_needs_preflight_before_provider_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _module()
    snapshot = tmp_path / "snapshot"
    mod.plan(snapshot)

    def forbidden_provider(*args: Any) -> None:
        del args
        pytest.fail("provider constructed before preflight")

    monkeypatch.setitem(mod.prepare.__globals__, "model_for", forbidden_provider)
    with pytest.raises(mod.StudyError, match="current_p5_preflight_required"):
        await mod.prepare(
            snapshot,
            tmp_path / "not_read_seed.json",
            tmp_path / "not_created",
            provider="deepseek",
            credentials=None,
        )
    assert not (tmp_path / "not_created").exists()


@pytest.mark.asyncio
async def test_p505_failed_preparation_records_terminal_attempt_without_review(
    tmp_path: Path,
) -> None:
    mod = _module()
    snapshot = tmp_path / "snapshot"
    mod.plan(snapshot)
    output = tmp_path / "attempt"
    with pytest.raises((mod.ext.AcceptanceError, OSError)):
        await mod.prepare(
            snapshot,
            tmp_path / "missing_seed.json",
            output,
            provider="scripted",
            credentials=None,
        )
    attempt = mod.read(output / "preparation_attempt.json")
    assert attempt["status"] == "failed"
    assert attempt["model_usage"] == []
    assert not (output / "preparation.json").exists()
    assert not (output / "authorization-template.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["preparation_provider", "preparation_manifest_sha256"])
async def test_p503_sealed_preparation_provenance_cannot_be_relabelled(
    tmp_path: Path, field: str
) -> None:
    mod = _module()
    snapshot, prepared = await _prepared(tmp_path, mod)
    authority = mod.read(prepared / "authorization-template.json")
    authority.update(
        authorization_kind="synthetic_fixture",
        approved_by="fixture_operator",
        approved_at="2026-11-12T00:00:00+00:00",
    )
    path = tmp_path / "authority.json"
    mod.save(path, authority)
    committed = tmp_path / "committed"
    seed = await mod.authorize(snapshot, prepared, path, committed, fixture=True)
    seed[field] = "deepseek" if field == "preparation_provider" else "0" * 64
    mod.save(committed / "seed.json", seed)
    with pytest.raises(mod.StudyError, match="sealed_preparation_scope_changed"):
        await mod.checked_seed(snapshot, committed, real=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["credentials", "reserved"])
async def test_p504_missing_key_or_previous_attempt_stops_before_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    mod = _module()
    snapshot = tmp_path / "snapshot"
    mod.plan(snapshot)
    _preflight_fixture(mod, snapshot, tmp_path / "preflight")
    empty_key = tmp_path / "empty_credentials"
    empty_key.write_text("")
    if condition == "reserved":
        (snapshot / "real_consolidation_reserved.json").write_text("terminal old attempt")

        def forbidden_provider(*args: Any) -> None:
            del args
            pytest.fail("provider constructed after previous attempt")

        monkeypatch.setitem(mod.prepare.__globals__, "model_for", forbidden_provider)
    reason = (
        "deepseek_credentials_required"
        if condition == "credentials"
        else "real_consolidation_already_reserved"
    )
    with pytest.raises(mod.StudyError, match=reason):
        await mod.prepare(
            snapshot,
            tmp_path / "not_read_seed.json",
            tmp_path / "not_created",
            provider="deepseek",
            credentials=empty_key,
            preflight=tmp_path / "preflight",
        )
    assert not (tmp_path / "not_created").exists()


@pytest.mark.asyncio
async def test_p504_505_controlled_provider_branch_audits_and_reserves_one_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _module()
    fixtures = runpy.run_path(str(ROOT / "tests/test_extension_acceptance.py"))
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = await fixtures["prepared_seed_fixture"](source_root)
    snapshot = tmp_path / "snapshot"
    mod.plan(snapshot)
    _preflight_fixture(mod, snapshot, tmp_path / "preflight")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "draft",
                        "propose_consolidated_note",
                        {
                            "kind": "environment",
                            "topic": "local_policy",
                            "trigger": "On project edits.",
                            "rule": "Use local tests; preserve preconditions and exceptions.",
                            "verification": "Run local checks.",
                            "supersede_sources": True,
                        },
                    ),
                )
            ),
            ModelResponse("Proposed for review."),
        ],
        measure_input_tokens=True,
    )
    constructors = []
    canary = sha256(str(tmp_path).encode()).hexdigest()

    def controlled_provider(*args: Any) -> Any:
        constructors.append(args)
        return mod.AuditedCredentialModel(model, canary)

    monkeypatch.setitem(mod.prepare.__globals__, "model_for", controlled_provider)
    output = tmp_path / "controlled_attempt"
    await mod.prepare(
        snapshot,
        source,
        output,
        provider="deepseek",
        credentials=None,
        preflight=tmp_path / "preflight",
    )
    assert len(constructors) == 1
    assert mod.read(output / "input_secret_audit.json")["corpus"]["matched_units"] == 0
    assert mod.read(output / "review.json")["selected"]["status"] == "proposed"
    assert (snapshot / "real_consolidation_reserved.json").is_file()
    with pytest.raises(mod.StudyError, match="real_consolidation_already_reserved"):
        await mod.prepare(
            snapshot,
            source,
            tmp_path / "second_attempt",
            provider="deepseek",
            credentials=None,
            preflight=tmp_path / "preflight",
        )
    assert len(constructors) == 1
    assert not (tmp_path / "second_attempt").exists()


@pytest.mark.asyncio
async def test_p507_report_retains_slot_and_unknown_usage_when_witness_is_missing(
    tmp_path: Path,
) -> None:
    mod = _module()
    snapshot = tmp_path / "snapshot"
    frozen = mod.plan(snapshot)
    execution_root = tmp_path / "execution"
    slot = frozen["protocol"]["queue"][0]
    directory = execution_root / "runs" / mod.ext.queue_id(0, slot)
    directory.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = build_local_runtime(
        workspace=workspace,
        model=ScriptedModel([ModelResponse("done", usage=TokenUsage(5, 2))]),
        options=RuntimeOptions(enabled_tools=frozenset()),
    )
    runtime.events.subscribe("*", JsonlTraceWriter(directory / "trace.jsonl"))
    try:
        result = await runtime.agent.run("Record a reporting fixture.", run_id="report_fixture")
    finally:
        if runtime.close is not None:
            await runtime.close()
    mod.save(
        execution_root / "execution.json",
        {
            "source_sha256": frozen["source_sha256"],
            "protocol_sha256": frozen["protocol_sha256"],
            "provider": "scripted",
            "checked_slots": 0,
            "safety_passed": None,
            "rows": [
                {
                    "index": 0,
                    **slot,
                    "directory": directory.relative_to(execution_root).as_posix(),
                    "run_id": result.run_id,
                    "provider_invocations": 1,
                    "known_tokens": 7,
                    "usage_complete": True,
                }
            ],
        },
    )
    report = mod.report(snapshot, execution_root, tmp_path / "derived")
    baseline = report["comparisons"]["groups"]["Baseline"]
    assert report["declared_slots"] == 90
    assert baseline["observed_runs"] == 1
    assert baseline["known_tokens"] == 7
    assert baseline["usage_complete_runs"] == 0
    assert report["benefit_evaluation"] == "not_assessable"
    assert report["evidence_index"]["0"]["usage_witness_sha256"] is None
    metadata = report["served_model_metadata"]["Baseline"]
    assert metadata["unknown_provider_models"] == metadata["responses"] == 1
