"""Freeze and execute the Memory-only study using the existing isolated executor."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import io
import json
import os
import re
import runpy
import shutil
import sys
import tarfile
import tempfile
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from minidsh.adapters import JsonlTraceWriter, ScriptedModel
from minidsh.adapters.credentials import CredentialStore, default_credentials_path
from minidsh.adapters.memory import SQLiteMemoryStore
from minidsh.adapters.memory_evidence import JsonMemoryEvidenceSource
from minidsh.adapters.models import CredentialRedactingModel, OpenAICompatibleModel
from minidsh.benchmark.memory_study import (
    ARM_GROUPS,
    MemoryArm,
    memory_study_options,
    study_protocol,
)
from minidsh.benchmark.runner import BenchmarkManifest
from minidsh.domain import ModelCallError, ModelResponse, ToolCall, ToolStatus
from minidsh.domain.memory import (
    MemoryConfig,
    MemoryEvidenceFile,
    MemoryEvidenceGrant,
    MemoryStatus,
)
from minidsh.domain.memory_notes import MemoryNoteSource, parse_memory_note, validate_note_evidence
from minidsh.evaluation.extension_comparison import (
    ComparisonReport,
    ComparisonRun,
    compare_extension_groups,
)
from minidsh.evaluation.harness_metrics import ScoringEvidence, score_harness
from minidsh.evaluation.tracefile import TraceError, TraceRun
from minidsh.infrastructure.containers import DockerHost
from minidsh.infrastructure.process import CommandPolicy, LocalCommandRunner
from minidsh.infrastructure.sandbox import WorkspaceSandbox
from minidsh.memory_consolidation import ConsolidateMemoryTool
from minidsh.runtime import EventBus
from minidsh.runtime.memory import MemoryService
from minidsh.runtime.memory_maintenance import memory_maintenance_tools
from minidsh.runtime.memory_note_tools import memory_note_maintenance_tools
from minidsh.tools import PermissionPolicy, ToolRegistry, ToolRuntime

ROOT = Path(__file__).resolve().parents[1]
ext = SimpleNamespace(**runpy.run_path(str(ROOT / "scripts/extension_acceptance.py")))
HASH = r"^[a-f0-9]{64}$"
INSTRUCTION = (
    "Propose exactly one concise environment policy derived only from the authorized notes. "
    "Preserve their precondition, exception and exported API contracts; use the existing local "
    "runtime and visible checks, and avoid network dependencies. Call propose_consolidated_note "
    "with kind=environment and supersede_sources=true. Keep trigger <=20 characters, rule <=120 "
    "characters and verification <=30, because full source references are added twice within the "
    "2KiB envelope. Then finish. Do not validate or approve."
)


class StudyError(RuntimeError):
    """A safe diagnostic that never contains provider or credential text."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class _Binding(_Record):
    entry_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    revision: int = Field(gt=0)
    content_sha256: str = Field(pattern=HASH)


class _Authority(_Record):
    schema_version: Literal["1.0"]
    purpose: Literal["memory_v2_p5_frozen_queue"]
    authorization_kind: Literal["synthetic_fixture", "explicit_user_approval"]
    protocol_sha256: str = Field(pattern=HASH)
    source_sha256: str = Field(pattern=HASH)
    review_sha256: str = Field(pattern=HASH)
    approved_by: str = Field(min_length=1, max_length=128)
    approved_at: str = Field(min_length=1, max_length=128)
    original: list[_Binding] = Field(min_length=3, max_length=3)
    consolidated: list[_Binding] = Field(min_length=1, max_length=1)


def read(path: Path) -> Any:
    return json.loads(ext.read_seed_file(path.absolute()))


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError("unsupported_frozen_value")


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=_json_default) + "\n")


def pin(path: Path) -> str:
    return ext.digest(ext.read_seed_file(path.absolute()))


def files_in(root: Path, *, excluding: frozenset[str] = frozenset()) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise StudyError("seed_symlink_denied")
        if path.is_file() and path.relative_to(root).as_posix() not in excluding:
            result[path.relative_to(root).as_posix()] = pin(path)
    return result


def source_inputs() -> dict[str, str]:
    return {
        name: value
        for name, value in ext.source_inputs(ROOT).items()
        if not name.startswith("docs/results/memory-v2-p5")
    }


def plan(output: Path) -> dict[str, Any]:
    protocol = study_protocol()
    files = source_inputs()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    archive = output / "source.tar.gz"
    with (
        archive.open("xb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        for name, expected in files.items():
            data = (ROOT / name).read_bytes()
            if ext.digest(data) != expected:
                raise StudyError("source_changed_during_freeze")
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    manifest = {
        "schema_version": "1.0",
        "protocol": protocol,
        "protocol_sha256": ext.digest(ext.canonical(protocol)),
        "source_files": files,
        "source_sha256": ext.digest(ext.canonical(files)),
        "source_archive_sha256": pin(archive),
        "scope": "Memory v2 P5; no old M5 authorization or results imported",
    }
    save(output / "manifest.json", manifest)
    return manifest


def check(snapshot: Path, *, current: bool = True) -> dict[str, Any]:
    manifest = read(snapshot / "manifest.json")
    if ext.canonical(manifest["protocol"]) != ext.canonical(study_protocol()):
        raise StudyError("frozen_protocol_changed")
    if manifest["protocol_sha256"] != ext.digest(ext.canonical(manifest["protocol"])):
        raise StudyError("protocol_hash_changed")
    files = manifest["source_files"]
    if manifest["source_sha256"] != ext.digest(ext.canonical(files)):
        raise StudyError("source_manifest_changed")
    if current and source_inputs() != files:
        raise StudyError("current_source_changed")
    if pin(snapshot / "source.tar.gz") != manifest["source_archive_sha256"]:
        raise StudyError("source_archive_changed")
    with tarfile.open(snapshot / "source.tar.gz") as archive:
        members = archive.getmembers()
        if len(members) != len(files) or {m.name for m in members} != set(files):
            raise StudyError("source_archive_scope_changed")
        for member in members:
            relative = PurePosixPath(member.name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not member.isfile()
                or member.size > 8 * 1024 * 1024
            ):
                raise StudyError("source_archive_path_denied")
            source = archive.extractfile(member)
            if source is None or ext.digest(source.read()) != files.get(member.name):
                raise StudyError("source_archive_content_changed")
    # Same task and independent-overlay predicates as the original executor.
    ext.check_protocol(read(ROOT / "benchmarks/extension_v1/manifest.json"), ROOT)
    return manifest


def secret_audit(root: Path, secret: str) -> dict[str, Any]:
    """Detect only the active exact credential, including compressed source members."""
    if not secret:
        raise StudyError("audit_credential_required")
    target = secret.encode()
    checked, archive_members, matches = 0, 0, 0
    paths = [root] if root.is_file() else sorted(root.rglob("*"))
    for path in paths:
        if path.is_symlink():
            raise StudyError("audit_symlink_denied")
        if not path.is_file():
            continue
        data = ext.read_seed_file(path.absolute())
        checked += 1
        matches += int(target in data)
        if path.name.endswith(".tar.gz"):
            with tarfile.open(path) as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    if member.size > 8 * 1024 * 1024:
                        raise StudyError("audit_archive_member_limit")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise StudyError("audit_archive_invalid")
                    archive_members += 1
                    matches += int(target in stream.read() or target in member.name.encode())
    return {
        "checked_files": checked,
        "checked_archive_members": archive_members,
        "matched_units": matches,
        "scope": "exact active credential only; no semantic or encoded-secret claim",
    }


class AuditedCredentialModel(CredentialRedactingModel):
    def audit(self, root: Path) -> dict[str, Any]:
        return secret_audit(root, self._secret)


def model_for(protocol: dict[str, Any], credentials: Path | None) -> Any:
    secret = ""
    if credentials is not None:
        for line in ext.read_seed_file(credentials).decode().splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "DEEPSEEK_API_KEY":
                secret = value.strip().strip("\"'")
    else:
        selection = CredentialStore(ROOT, default_credentials_path()).resolve(
            "DEEPSEEK_API_KEY", os.environ
        )
        secret = selection.value or ""
    if not secret:
        raise StudyError("deepseek_credentials_required")
    model = protocol["model"]
    return AuditedCredentialModel(
        OpenAICompatibleModel(
            endpoint=model["endpoint"],
            model=model["id"],
            api_key=secret,
            temperature=model["temperature"],
            thinking=model["thinking"],
            max_attempts=1,
            max_output_tokens=model["max_output_tokens"],
            require_usage=True,
        ),
        secret,
    )


def binding(entry: Any) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "revision": entry.revision,
        "content_sha256": entry.content_sha256,
    }


async def operator_tools(
    config: MemoryConfig, workspace: Path, trace: Path, operator: str
) -> tuple[Any, Any, Any]:
    store = SQLiteMemoryStore(
        Path(config.database), allowed_root=Path(config.storage_root), workspace=workspace
    )
    evidence = JsonMemoryEvidenceSource(
        Path(config.evidence_root), workspace=workspace, grants=config.evidence_grants
    )
    service = MemoryService(
        store,
        project_id=config.project_id,
        applicable_version=config.applicable_version,
        evidence=evidence,
        config=config,
        forbidden_content_sha256=config.forbidden_content_sha256,
    )
    bus = EventBus()
    bus.subscribe("*", JsonlTraceWriter(trace))
    registry = ToolRegistry()
    for tool in (
        *memory_maintenance_tools(service, operator_id=operator),
        *memory_note_maintenance_tools(service, operator_id=operator),
    ):
        registry.register(tool)
    tools = ToolRuntime(
        registry,
        PermissionPolicy(frozenset({"memory.maintain", "memory.migrate", "memory.evidence.read"})),
        bus,
    )
    return store, service, (bus, registry, tools)


async def call(tools: Any, name: str, arguments: dict[str, Any], *, step: int) -> Any:
    observed = await tools.execute(
        ToolCall(f"operator_{step}", name, arguments), "seed_operator", step
    )
    if observed.status is not ToolStatus.SUCCEEDED or observed.truncated:
        raise StudyError("seed_maintenance_failed")
    return observed.result


async def original_config(bundle: Path) -> MemoryConfig:
    profile = await ext.load_seed(
        bundle / "original/memory_seed.json",
        workspace=bundle / "audit_workspace",
        frozen_snapshot=bundle / "original",
    )
    return profile.memory


def candidate_config(original: MemoryConfig, bundle: Path) -> MemoryConfig:
    return replace(
        original,
        database=str(bundle / "candidate/memory.sqlite3"),
        storage_root=str(bundle / "candidate"),
        retrieval_mode="progressive_v2",
    )


async def prepare(  # noqa: PLR0913 - explicit preparation inputs
    snapshot: Path,
    source_seed: Path,
    output: Path,
    *,
    provider: str,
    credentials: Path | None,
    preflight: Path | None = None,
) -> dict[str, Any]:
    manifest = check(snapshot)
    if provider == "deepseek":
        check_preflight(snapshot, preflight)
        reservation = snapshot / "real_consolidation_reserved.json"
        if reservation.exists() or reservation.is_symlink():
            raise StudyError("real_consolidation_already_reserved")
    if await asyncio.to_thread(output.exists):
        raise StudyError("new_preparation_directory_required")
    # Resolve credentials without issuing a completion before creating a real attempt.
    model = (
        model_for(manifest["protocol"], credentials)
        if provider == "deepseek"
        else ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "draft",
                            "propose_consolidated_note",
                            {
                                "kind": "environment",
                                "topic": "project_execution_policy",
                                "trigger": "On project edits.",
                                "rule": (
                                    "Preserve boundary/exception contracts and APIs. "
                                    "Use the local runtime and visible tests; no network."
                                ),
                                "verification": ("Run visible checks."),
                                "supersede_sources": True,
                            },
                        ),
                    )
                ),
                ModelResponse("A proposed policy is ready for operator review."),
            ],
            measure_input_tokens=True,
        )
    )
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False, mode=0o700)
    save(
        output / "preparation_attempt.json",
        {
            "source_sha256": manifest["source_sha256"],
            "protocol_sha256": manifest["protocol_sha256"],
            "provider": provider,
            "status": "running",
            "budget": manifest["protocol"]["consolidation_budget"],
        },
    )
    model = UsageObservedModel(model)
    try:
        return await _prepare_materials(
            snapshot, source_seed, output, manifest=manifest, model=model, provider=provider
        )
    except BaseException as exc:
        save(
            output / "preparation_attempt.json",
            {
                "source_sha256": manifest["source_sha256"],
                "protocol_sha256": manifest["protocol_sha256"],
                "provider": provider,
                "status": "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                "error_type": type(exc).__name__,
                "model_usage": model.usage,
                "note": "Terminal attempt; inspect raw trace. No automatic retry or approval.",
            },
        )
        raise
    finally:
        save(output / "preparation_model_usage.json", model.usage)


async def _prepare_materials(  # noqa: PLR0913, PLR0915 - ordered private material preparation
    snapshot: Path,
    source_seed: Path,
    output: Path,
    *,
    manifest: dict[str, Any],
    model: UsageObservedModel,
    provider: str,
) -> dict[str, Any]:
    (output / "original").mkdir(mode=0o700)
    (output / "audit_workspace").mkdir(mode=0o700)
    frozen_original = await asyncio.to_thread(ext.freeze_seed, source_seed, output / "original")
    original = await original_config(output)
    (output / "candidate").mkdir(mode=0o700)
    await asyncio.to_thread(
        ext.write_private,
        output / "candidate/memory.sqlite3",
        ext.read_seed_file(Path(original.database), pin(Path(original.database))),
    )
    if provider == "deepseek":
        audit = model.audit(output / "original")
        audit_source = model.audit(snapshot)
        save(output / "input_secret_audit.json", {"corpus": audit, "source": audit_source})
        if audit["matched_units"] or audit_source["matched_units"]:
            raise StudyError("secret_in_frozen_inputs")
    config = candidate_config(original, output)
    store, service, (bus, registry, tools) = await operator_tools(
        config,
        output / "audit_workspace",
        output / "maintenance.jsonl",
        "fixture_operator" if provider == "scripted" else "preparation_operator",
    )
    try:
        before = await store.snapshot(config.project_id)
        await call(tools, "migrate_memory_history", {}, step=1)
        tool = ConsolidateMemoryTool(
            service,
            model_factory=lambda: model,
            events=bus,
            operator_id="fixture_operator" if provider == "scripted" else "preparation_operator",
            task_instruction=INSTRUCTION,
        )
        registry.register(tool)
        runtime = ToolRuntime(
            registry,
            PermissionPolicy(frozenset(tool.definition.permissions)),
            bus,
            max_output_chars=20000,
        )
        if provider == "deepseek":
            ext.write_private(
                snapshot / "real_consolidation_reserved.json",
                ext.canonical(
                    {
                        "source_sha256": manifest["source_sha256"],
                        "protocol_sha256": manifest["protocol_sha256"],
                        "output": str(output),
                        "purpose": "one explicit consolidation attempt; no automatic restart",
                    }
                ),
            )
        observed = await runtime.execute(
            ToolCall(
                "consolidate",
                "consolidate_memory",
                {
                    "materials_authorized": True,
                    "redacted": True,
                    "sources": [asdict(MemoryNoteSource.from_entry(e)) for e in before],
                    **manifest["protocol"]["consolidation_budget"],
                },
            ),
            "seed_preparation",
            2,
        )
        if observed.status is not ToolStatus.SUCCEEDED or not isinstance(observed.result, dict):
            raise StudyError("consolidation_preparation_failed")
        result = observed.result
        selected = None
        expected_sources = tuple(MemoryNoteSource.from_entry(e) for e in before)
        for receipt in result["candidates"]:
            candidate = await store.get(config.project_id, receipt["entry_id"])
            note = parse_memory_note(candidate.content) if candidate else None
            if (
                candidate is not None
                and candidate.kind.value == "environment"
                and note is not None
                and note.supersedes == expected_sources
            ):
                selected = candidate
                break
        if selected is None:
            raise StudyError("no_eligible_consolidation_proposal")
        for entry in before:
            if await store.get(config.project_id, entry.entry_id) != entry:
                raise StudyError("original_knowledge_changed_during_preparation")
        review = {
            "schema_version": "1.0",
            "protocol_sha256": manifest["protocol_sha256"],
            "source_sha256": manifest["source_sha256"],
            "provider": provider,
            "original": [
                {**asdict(e), **binding(e), "proposal_sha256": e.proposal_sha256} for e in before
            ],
            "selected": {
                **asdict(selected),
                **binding(selected),
                "proposal_sha256": selected.proposal_sha256,
            },
            "consolidation": result,
            "instruction": INSTRUCTION,
            "original_seed": frozen_original,
            "note": (
                "All new content remains proposed. This is not queue approval "
                "or independent task acceptance."
            ),
        }
        save(output / "review.json", review)
        text = "# Memory v2 P5 seed review\n\n"
        text += (
            f"Protocol: {manifest['protocol_sha256']}\n"
            f"Source: {manifest['source_sha256']}\nProvider: {provider}\n\n"
        )
        for entry in (*before, selected):
            text += (
                f"## {entry.entry_id}\n\nRevision {entry.revision}; status {entry.status.value}; "
                f"content SHA-256 {entry.content_sha256}\n\n{entry.content}\n\n"
            )
        (output / "review.md").write_text(text)
    finally:
        save(output / "preparation_model_usage.json", model.usage)
        await store.close()
    save(
        output / "preparation_attempt.json",
        {
            "source_sha256": manifest["source_sha256"],
            "protocol_sha256": manifest["protocol_sha256"],
            "provider": provider,
            "status": "proposed_review_ready",
            "usage": preparation_usage(output, review),
        },
    )
    save(output / "authorization-template.json", authority_template(output, review))
    preparation = {
        "schema_version": "1.0",
        "provider": provider,
        "protocol_sha256": manifest["protocol_sha256"],
        "source_sha256": manifest["source_sha256"],
        "review_sha256": pin(output / "review.json"),
        "review_markdown_sha256": pin(output / "review.md"),
        "files": files_in(output, excluding=frozenset({"preparation.json"})),
    }
    save(output / "preparation.json", preparation)
    return preparation


def verify_preparation(
    snapshot: Path, bundle: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    frozen, preparation = check(snapshot), read(bundle / "preparation.json")
    if (
        preparation["source_sha256"] != frozen["source_sha256"]
        or preparation["protocol_sha256"] != frozen["protocol_sha256"]
    ):
        raise StudyError("preparation_scope_changed")
    if files_in(bundle, excluding=frozenset({"preparation.json"})) != preparation["files"]:
        raise StudyError("preparation_files_changed")
    review = read(bundle / "review.json")
    if pin(bundle / "review.json") != preparation["review_sha256"]:
        raise StudyError("seed_review_changed")
    return frozen, preparation, review


def authority_template(snapshot: Path, review: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "purpose": "memory_v2_p5_frozen_queue",
        "authorization_kind": "explicit_user_approval",
        "protocol_sha256": review["protocol_sha256"],
        "source_sha256": review["source_sha256"],
        "review_sha256": pin(snapshot / "review.json"),
        "approved_by": "PENDING",
        "approved_at": "PENDING",
        "original": [
            {k: e[k] for k in ("entry_id", "revision", "content_sha256")}
            for e in review["original"]
        ],
        "consolidated": [
            {k: review["selected"][k] for k in ("entry_id", "revision", "content_sha256")}
        ],
    }


async def authorize(  # noqa: PLR0915 - ordered immutable seed publication
    snapshot: Path, bundle: Path, authority_file: Path, output: Path, *, fixture: bool = False
) -> dict[str, Any]:
    frozen, preparation, review = verify_preparation(snapshot, bundle)
    authority = _Authority.model_validate_json(ext.read_seed_file(authority_file))
    template = authority_template(bundle, review)
    values = authority.model_dump()
    if any(
        values[k] != template[k]
        for k in ("protocol_sha256", "source_sha256", "review_sha256", "original", "consolidated")
    ):
        raise StudyError("explicit_seed_authorization_mismatch")
    if authority.approved_by.strip().lower() in {"pending", "none", "unknown"}:
        raise StudyError("explicit_user_approval_required")
    if fixture:
        if (
            authority.authorization_kind != "synthetic_fixture"
            or preparation["provider"] != "scripted"
        ):
            raise StudyError("fixture_authorization_scope_mismatch")
    elif (
        authority.authorization_kind != "explicit_user_approval"
        or preparation["provider"] != "deepseek"
        or "fixture" in authority.approved_by.lower()
    ):
        raise StudyError("real_seed_preparation_and_explicit_approval_required")
    if review["consolidation"]["unknown_usage_calls"] != 0:
        raise StudyError("unknown_preparation_usage")
    # A new immutable copy; the review package and original database are never rewritten.
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False, mode=0o700)
    shutil.copytree(bundle / "original", output / "original")
    (output / "audit_workspace").mkdir(mode=0o700)
    (output / "candidate").mkdir(mode=0o700)
    ext.write_private(
        output / "candidate/memory.sqlite3", ext.read_seed_file(bundle / "candidate/memory.sqlite3")
    )
    shutil.copytree(bundle / "original/memory_evidence", output / "evidence")
    original = await original_config(output)
    selected = review["selected"]
    policy = {
        "purpose": "Explicit operator configuration of the selected environment policy",
        "entry_id": selected["entry_id"],
        "revision": selected["revision"],
        "content_sha256": selected["content_sha256"],
        "input_sha256": review["consolidation"]["input_sha256"],
        "protocol_sha256": frozen["protocol_sha256"],
        "configuration_kind": "project_policy",
        "note": "Authorization of preferred policy is not a hidden task acceptance verdict.",
    }
    evidence_dir = output / "evidence/consolidated_policy"
    evidence_dir.mkdir(mode=0o700)
    save(evidence_dir / "configuration.json", policy)
    document = {
        "schema_version": "1.0",
        "kind": "operator_configuration",
        "project_id": original.project_id,
        "applicable_version": original.applicable_version,
        "source_run_id": selected["source_run_id"],
        "configuration": {
            "path": "consolidated_policy/configuration.json",
            "sha256": pin(evidence_dir / "configuration.json"),
        },
        "confirmed_by": authority.approved_by,
        "confirmed_at": authority.approved_at,
    }
    save(evidence_dir / "bundle.json", document)
    grant = MemoryEvidenceGrant(
        "consolidated_policy",
        MemoryEvidenceFile("consolidated_policy/bundle.json", pin(evidence_dir / "bundle.json")),
    )
    config = replace(
        candidate_config(original, output),
        evidence_root=str(output / "evidence"),
        evidence_grants=(*original.evidence_grants, grant),
    )
    store, _, (_, _, tools) = await operator_tools(
        config,
        output / "audit_workspace",
        output / "authorization_trace.jsonl",
        authority.approved_by,
    )
    try:
        target = await store.get(config.project_id, selected["entry_id"])
        if (
            target is None
            or binding(target) != template["consolidated"][0]
            or target.status is not MemoryStatus.PROPOSED
        ):
            raise StudyError("selected_proposal_changed")
        await call(
            tools,
            "validate_memory",
            {
                "entry_id": target.entry_id,
                "expected_revision": target.revision,
                "evidence_id": grant.evidence_id,
            },
            step=1,
        )
        approved = await call(
            tools,
            "approve_memory",
            {
                "entry_id": target.entry_id,
                "expected_revision": target.revision,
                "content_sha256": target.content_sha256,
                "approved_at": authority.approved_at,
            },
            step=2,
        )
        await call(
            tools,
            "commit_memory",
            {
                "entry_id": target.entry_id,
                "expected_revision": target.revision,
                "approval_sha256": approved["approval_sha256"],
            },
            step=3,
        )
        rows = await store.snapshot(config.project_id)
        current = [e for e in rows if e.status is MemoryStatus.COMMITTED]
        if [binding(e) for e in current] != template["consolidated"]:
            raise StudyError("consolidated_corpus_scope_changed")
        if not all(
            e.status is MemoryStatus.SUPERSEDED
            for e in rows
            if e.entry_id in {r["entry_id"] for r in review["original"]}
        ):
            raise StudyError("supersession_not_atomic")
    finally:
        await store.close()
    save(output / "authorization.json", values)
    save(output / "review.json", review)
    shutil.copyfile(bundle / "review.md", output / "review.md")
    shutil.copyfile(bundle / "maintenance.jsonl", output / "preparation_trace.jsonl")
    shutil.copyfile(bundle / "preparation.json", output / "preparation_manifest.json")
    seed = {
        "schema_version": "1.0",
        "protocol_sha256": frozen["protocol_sha256"],
        "source_sha256": frozen["source_sha256"],
        "preparation_provider": preparation["provider"],
        "preparation_root": str(bundle),
        "preparation_manifest_sha256": pin(bundle / "preparation.json"),
        "preparation_tokens": preparation_usage(output, review, sealed=True)["known_tokens"],
        "authorization_kind": authority.authorization_kind,
        "authorization_sha256": pin(output / "authorization.json"),
        "original_bindings": template["original"],
        "consolidated_bindings": template["consolidated"],
        "consolidated_grant": asdict(grant),
        "database_sha256": {
            "original": pin(output / "original/memory_seed.sqlite3"),
            "consolidated": pin(output / "candidate/memory.sqlite3"),
        },
        "files": files_in(output, excluding=frozenset({"seed.json", "real_queue_reserved.json"})),
    }
    save(output / "seed.json", seed)
    return seed


async def checked_seed(  # noqa: PLR0912 - verify both frozen corpora before provider access
    snapshot: Path, bundle: Path, *, real: bool
) -> tuple[dict[str, Any], dict[str, Any], dict[str, MemoryConfig]]:
    frozen, seed = check(snapshot), read(bundle / "seed.json")
    if (
        seed["source_sha256"] != frozen["source_sha256"]
        or seed["protocol_sha256"] != frozen["protocol_sha256"]
    ):
        raise StudyError("approved_seed_scope_changed")
    if (
        files_in(bundle, excluding=frozenset({"seed.json", "real_queue_reserved.json"}))
        != seed["files"]
    ):
        raise StudyError("approved_seed_files_changed")
    if seed["authorization_sha256"] != pin(bundle / "authorization.json"):
        raise StudyError("seed_authorization_changed")
    authority = _Authority.model_validate_json(ext.read_seed_file(bundle / "authorization.json"))
    review = read(bundle / "review.json")
    prepared = read(bundle / "preparation_manifest.json")
    if (
        pin(bundle / "preparation_manifest.json") != seed["preparation_manifest_sha256"]
        or prepared["provider"] != seed["preparation_provider"]
        or prepared["provider"] != review["provider"]
        or prepared["source_sha256"] != frozen["source_sha256"]
        or prepared["protocol_sha256"] != frozen["protocol_sha256"]
        or prepared["review_sha256"] != pin(bundle / "review.json")
    ):
        raise StudyError("sealed_preparation_scope_changed")
    if authority.review_sha256 != pin(bundle / "review.json"):
        raise StudyError("authorized_review_changed")
    if (
        authority.source_sha256 != frozen["source_sha256"]
        or authority.protocol_sha256 != frozen["protocol_sha256"]
        or review["source_sha256"] != frozen["source_sha256"]
        or review["protocol_sha256"] != frozen["protocol_sha256"]
        or [b.model_dump() for b in authority.original] != seed["original_bindings"]
        or [b.model_dump() for b in authority.consolidated] != seed["consolidated_bindings"]
    ):
        raise StudyError("approved_binding_scope_changed")
    if real and (
        authority.authorization_kind != "explicit_user_approval"
        or seed["preparation_provider"] != "deepseek"
        or seed["authorization_kind"] != "explicit_user_approval"
        or "fixture" in authority.approved_by.lower()
    ):
        raise StudyError("explicit_user_seed_approval_required")
    original = await original_config(bundle)
    original_approval = read(bundle / "original/memory_approval.json")
    if real and original_approval["authorization_kind"] != "explicit_user_approval":
        raise StudyError("original_seed_is_synthetic")
    grant = MemoryEvidenceGrant(
        seed["consolidated_grant"]["evidence_id"],
        MemoryEvidenceFile(**seed["consolidated_grant"]["document"]),
    )
    consolidated = replace(
        candidate_config(original, bundle),
        evidence_root=str(bundle / "evidence"),
        evidence_grants=(*original.evidence_grants, grant),
    )
    for kind, config in (("original", original), ("consolidated", consolidated)):
        store = SQLiteMemoryStore(
            Path(config.database),
            allowed_root=Path(config.storage_root),
            workspace=bundle / "audit_workspace",
        )
        source = JsonMemoryEvidenceSource(
            Path(config.evidence_root),
            workspace=bundle / "audit_workspace",
            grants=config.evidence_grants,
        )
        try:
            rows = await store.snapshot(config.project_id)
            committed = [e for e in rows if e.status is MemoryStatus.COMMITTED]
            if [binding(e) for e in committed] != seed[kind + "_bindings"]:
                raise StudyError("approved_entries_changed")
            for entry in committed:
                proof = await source.resolve_sha256(entry.evidence_sha256)
                proposal = replace(
                    entry,
                    status=MemoryStatus.PROPOSED,
                    validation_sha256=None,
                    approved_by=None,
                    approved_at=None,
                    approved_revision=None,
                    approval_sha256=None,
                )
                if validate_note_evidence(proposal, proof).proposal_sha256 != entry.proposal_sha256:
                    raise StudyError("approved_evidence_changed")
            if pin(Path(config.database)) != seed["database_sha256"][kind]:
                raise StudyError("approved_database_changed")
        finally:
            await store.close()
    audited = preparation_usage(bundle, review, sealed=True)
    if audited["known_tokens"] != seed["preparation_tokens"]:
        raise StudyError("preparation_usage_changed")
    if real and (not audited["usage_complete"] or not audited["finalized"]):
        raise StudyError("preparation_not_auditable")
    return frozen, seed, {"original": original, "consolidated": consolidated}


def preparation_usage(
    bundle: Path, review: dict[str, Any], *, sealed: bool = False
) -> dict[str, Any]:
    trace_path = bundle / ("preparation_trace.jsonl" if sealed else "maintenance.jsonl")
    trace = TraceRun.from_file(trace_path, run_id=review["consolidation"]["run_id"])
    score = score_harness(trace)
    values = score.measurements
    started = trace.named("memory_consolidation_started")
    if (
        started is None
        or started.data.get("input_sha256") != review["consolidation"]["input_sha256"]
    ):
        raise StudyError("consolidation_input_hash_not_traced")
    return {
        "trace_sha256": pin(trace_path),
        "run_id": review["consolidation"]["run_id"],
        "known_tokens": values["total_tokens"],
        "usage_complete": values["token_usage_complete"],
        "unknown_usage_calls": values["unknown_usage_calls"],
        "finalized": values["finalized"],
        "responses": len(trace.by_name("model_call_completed")),
        "provider_models": sorted(
            {
                str(r.data["provider_model"])
                for r in trace.by_name("model_call_completed")
                if r.data.get("provider_model")
            }
        ),
        "system_fingerprints": sorted(
            {
                str(r.data["system_fingerprint"])
                for r in trace.by_name("model_call_completed")
                if r.data.get("system_fingerprint")
            }
        ),
    }


def check_preflight(snapshot: Path, preflight: Path | None) -> dict[str, Any]:
    frozen = check(snapshot)
    if preflight is None:
        raise StudyError("current_p5_preflight_required")
    proof = read(preflight / "preflight.json")
    if (
        proof["source_sha256"] != frozen["source_sha256"]
        or proof["protocol_sha256"] != frozen["protocol_sha256"]
    ):
        raise StudyError("preflight_source_changed")
    if (
        proof["passed"] is not True
        or files_in(preflight, excluding=frozenset({"preflight.json"})) != proof["files"]
    ):
        raise StudyError("preflight_evidence_changed")
    if proof["offline_checked_slots"] != frozen["protocol"]["real_model_runs"]:
        raise StudyError("preflight_cohort_incomplete")
    if proof.get("native_sha256") != check_native_probes(frozen, preflight):
        raise StudyError("preflight_native_binding_changed")
    contracts = read(preflight / "contracts.json")
    if (
        proof.get("contracts_sha256") != pin(preflight / "contracts.json")
        or contracts["source_sha256"] != frozen["source_sha256"]
        or contracts["protocol_sha256"] != frozen["protocol_sha256"]
        or contracts["passed"] is not True
        or not contracts["rows"]
        or any(r["status"] != "passed" for r in contracts["rows"])
    ):
        raise StudyError("preflight_contracts_invalid")
    reconstruction = read(preflight / "reconstruction.json")
    if (
        proof.get("reconstruction_sha256") != pin(preflight / "reconstruction.json")
        or reconstruction["source_sha256"] != frozen["source_sha256"]
        or reconstruction["source_archive_sha256"] != frozen["source_archive_sha256"]
        or reconstruction["source_files"] != len(frozen["source_files"])
        or reconstruction["exit_code"] != 0
        or reconstruction["timed_out"] is not False
        or reconstruction["log_sha256"] != pin(preflight / "reconstruction.log")
    ):
        raise StudyError("preflight_reconstruction_invalid")
    return proof


def probe_module() -> SimpleNamespace:
    return SimpleNamespace(**runpy.run_path(str(ROOT / "scripts/memory_study_probes.py")))


def check_native_probes(frozen: dict[str, Any], preflight: Path) -> dict[str, str]:
    probes = probe_module()
    hashes = {}
    for name, cases in (("mechanisms", probes.MECHANISM_CASES), ("os", probes.OS_CASES)):
        directory = preflight / "native" / name
        if not (directory / "checks.json").is_file() or not (directory / "criteria.json").is_file():
            raise StudyError("preflight_native_evidence_required")
        result, criteria = read(directory / "checks.json"), read(directory / "criteria.json")
        expected_driver = frozen["source_files"]["scripts/memory_study_probes.py"]
        if (
            result["source_sha256"] != frozen["source_sha256"]
            or result["protocol_sha256"] != frozen["protocol_sha256"]
            or criteria["source_sha256"] != frozen["source_sha256"]
            or criteria["protocol_sha256"] != frozen["protocol_sha256"]
            or result["driver_sha256"] != expected_driver
            or criteria["driver_sha256"] != expected_driver
            or result["criteria_sha256"] != pin(directory / "criteria.json")
            or set(criteria["cases"]) != cases
            or set(result["checks"]) != cases
            or result["passed"] is not True
            or type(result["real_model_calls"]) is not int
            or result["real_model_calls"] != 0
            or not all(
                bool(row) and all(v is True for v in row.values())
                for row in result["checks"].values()
            )
        ):
            raise StudyError("preflight_native_evidence_invalid")
        hashes[name] = pin(directory / "checks.json")
    return hashes


class UsageObservedModel(ext.ObservedModel):
    """Independent usage witness; never change a request or automatically retry."""

    def __init__(self, inner: Any) -> None:
        super().__init__(inner)
        self.usage: list[dict[str, Any]] = []

    def audit(self, root: Path) -> dict[str, Any]:
        if not isinstance(self.inner, AuditedCredentialModel):
            raise StudyError("active_credential_audit_unavailable")
        return self.inner.audit(root)

    async def complete(self, request: Any) -> Any:
        self.calls += 1
        self.requests.append(request)
        try:
            response = await self.inner.complete(request)
        except BaseException as exc:
            used = exc.usage if isinstance(exc, ModelCallError) else None
            self.usage.append(
                {
                    "input_tokens": used.input_tokens if used else 0,
                    "output_tokens": used.output_tokens if used else 0,
                    "usage_known": used is not None,
                    "provider_model": None,
                    "system_fingerprint": None,
                }
            )
            raise
        self.usage.append(
            {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "usage_known": response.usage_known,
                "provider_model": response.provider_model,
                "system_fingerprint": response.system_fingerprint,
            }
        )
        return response


def usage_metrics(directory: Path, model: UsageObservedModel) -> dict[str, Any]:
    return witness_metrics(directory, model.usage, calls=model.calls)


def witness_metrics(directory: Path, usage: list[dict[str, Any]], *, calls: int) -> dict[str, Any]:
    port_tokens = sum(r["input_tokens"] + r["output_tokens"] for r in usage)
    port_unknown = sum(not r["usage_known"] for r in usage) + max(0, calls - len(usage))
    values = {
        "total_tokens": 0,
        "token_usage_complete": False,
        "unknown_usage_calls": 0,
        "finalized": False,
    }
    try:
        trace = TraceRun.from_file(directory / "trace.jsonl")
        values.update(score_harness(trace).measurements)
    except (TraceError, ValueError, OSError):
        pass
    if values["total_tokens"] != port_tokens:
        values["token_usage_complete"] = False
    values["total_tokens"] = max(values["total_tokens"], port_tokens)
    values["unknown_usage_calls"] = max(values["unknown_usage_calls"], port_unknown)
    values["token_usage_complete"] = (
        values["token_usage_complete"] and port_unknown == 0 and len(usage) == calls
    )
    return values


def recount(execution: dict[str, Any]) -> None:
    execution["known_tokens"] = execution["preparation_tokens"] + sum(
        r["known_tokens"] for r in execution["rows"]
    )
    execution["unknown_usage_calls"] = sum(
        r.get("unknown_usage_calls", 0) for r in execution["rows"]
    )


def record_partial(execution: dict[str, Any], active: Any, output: Path) -> None:
    if active is None:
        return
    number, slot, directory, model = active
    if any(r["index"] == number for r in execution["rows"]):
        return
    values = usage_metrics(directory, model)
    save(directory / "model_usage.json", model.usage)
    facts = read(directory / "check.json") if (directory / "check.json").is_file() else {}
    checked = facts.get("status") == "checked"
    execution["checked_slots"] += int(checked)
    execution["rows"].append(
        {
            "index": number,
            **slot,
            "directory": directory.relative_to(output).as_posix(),
            "run_id": facts.get("run_id"),
            "known_tokens": values["total_tokens"],
            "usage_complete": values["token_usage_complete"],
            "unknown_usage_calls": values["unknown_usage_calls"],
            "finalized": values["finalized"],
            "task_success": facts.get("task_success"),
            "provider_invocations": model.calls,
            "interrupted_or_unverified": True,
        }
    )
    execution["safety_passed"] = None
    recount(execution)


async def run(  # noqa: PLR0913, PLR0912, PLR0915 - fixed sequential cohort lifecycle
    snapshot: Path,
    bundle: Path,
    output: Path,
    *,
    provider: str,
    credentials: Path | None = None,
    preflight: Path | None = None,
) -> dict[str, Any]:
    real = provider == "deepseek"
    frozen, seed, configs = await checked_seed(snapshot, bundle, real=real)
    if real:
        check_preflight(snapshot, preflight)
    reservation = bundle / "real_queue_reserved.json"
    if real and (reservation.exists() or reservation.is_symlink()):
        raise StudyError("real_queue_already_reserved")
    if await asyncio.to_thread(output.exists):
        raise StudyError("new_execution_directory_required")
    protocol = frozen["protocol"]
    host = DockerHost()
    native = await host.run(("docker", "info", "--format", "{{.Architecture}}"))
    if native.exit_code or native.stdout.strip() not in {"aarch64", "arm64"}:
        raise StudyError("native_container_architecture_required")
    for image in protocol["isolation"]["images"]["images"].values():
        inspected = await host.run(("docker", "image", "inspect", image))
        if inspected.exit_code:
            raise StudyError("pinned_image_missing")
        ext.checked_image_size(inspected.stdout, image, "arm64")
    shared = model_for(protocol, credentials) if real else None
    if shared is not None and (
        shared.audit(snapshot)["matched_units"] or shared.audit(bundle)["matched_units"]
    ):
        raise StudyError("secret_in_frozen_inputs")
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False, mode=0o700)
    if real:
        ext.write_private(
            reservation,
            ext.canonical(
                {
                    "source_sha256": frozen["source_sha256"],
                    "protocol_sha256": frozen["protocol_sha256"],
                    "output": str(output),
                    "purpose": "one fixed queue; no automatic restart",
                }
            ),
        )
    tasks = {
        t.task_id: t for t in BenchmarkManifest.from_file(ROOT / protocol["source_suite"]).tasks
    }
    execution = {
        "schema_version": "1.0",
        "source_sha256": frozen["source_sha256"],
        "protocol_sha256": frozen["protocol_sha256"],
        "seed_sha256": pin(bundle / "seed.json"),
        "provider": provider,
        "status": "running",
        "declared_slots": len(protocol["queue"]),
        "checked_slots": 0,
        "rows": [],
        "known_tokens": seed["preparation_tokens"],
        "attempted_slots": 0,
        "usage_basis": "reported_api_usage" if real else "scripted_prompt_estimates",
        "unknown_usage_calls": 0,
        "preparation_tokens": seed["preparation_tokens"],
        "total_token_budget": protocol["total_token_budget"],
        "stop_reason": None,
        "controller_pid": os.getpid(),
        "safety_passed": None,
    }
    save(output / "execution.json", execution)
    active = None
    try:
        for index, slot in enumerate(protocol["queue"]):
            remaining = protocol["total_token_budget"] - execution["known_tokens"]
            if remaining < protocol["budget"]["max_tokens"]:
                execution["stop_reason"] = "insufficient_total_budget_for_unchanged_run_limit"
                break
            await checked_seed(snapshot, bundle, real=real)
            arm = ARM_GROUPS[slot["group"]]
            corpus = "consolidated" if arm is MemoryArm.CONSOLIDATED else "original"
            memory = None if arm is MemoryArm.OFF else configs[corpus]
            options = memory_study_options(arm, memory)
            directory = output / "runs" / ext.queue_id(index, slot)
            directory.mkdir(parents=True, exist_ok=False)
            task = tasks[slot["case_id"]]
            model = UsageObservedModel(
                shared if real else ScriptedModel.from_file(task.response_script)
            )
            save(
                directory / "p5_configuration.json",
                {
                    "arm": arm.value,
                    "slot": slot,
                    "options": asdict(options),
                    "source_sha256": frozen["source_sha256"],
                    "protocol_sha256": frozen["protocol_sha256"],
                },
            )
            execution["attempted_slots"] += 1
            active = (index, slot, directory, model)
            save(output / "execution.json", execution)
            try:
                facts = await ext.isolated_row(
                    snapshot,
                    directory,
                    slot,
                    task,
                    protocol,
                    host,
                    model,
                    remaining,
                    runtime_options=options,
                    memory_database_sha256=None
                    if memory is None
                    else seed["database_sha256"][corpus],
                )
            finally:
                save(directory / "model_usage.json", model.usage)
            trace = TraceRun.from_file(directory / "trace.jsonl")
            sidecar = ScoringEvidence.model_validate_json(
                (directory / "evidence.json").read_bytes()
            )
            started = trace.named("run_started")
            if (
                started is None
                or started.run_id != facts["run_id"]
                or sidecar.run_id != facts["run_id"]
                or sidecar.case_id != slot["case_id"]
            ):
                raise StudyError("run_sidecar_scope_mismatch")
            values = usage_metrics(directory, model)
            unchanged = (
                memory is None
                or pin(directory / "private/memory.sqlite3") == seed["database_sha256"][corpus]
            )
            safety = {
                "memory_database_unchanged": unchanged,
                "actor_removed": facts["actor_removed"],
                "evaluator_recorded": (directory / "evaluator_container.json").is_file()
                if facts["artifact_captured"]
                else False,
            }
            if shared is not None:
                audit = shared.audit(directory)
                save(directory / "secret_audit.json", audit)
                safety["active_credential_absent"] = audit["matched_units"] == 0
            save(directory / "p5_safety.json", safety)
            execution["safety_passed"] = True if all(safety.values()) else None
            row = {
                "index": index,
                **slot,
                "directory": directory.relative_to(output).as_posix(),
                "run_id": facts["run_id"],
                "known_tokens": values["total_tokens"],
                "usage_complete": values["token_usage_complete"],
                "unknown_usage_calls": values["unknown_usage_calls"],
                "finalized": values["finalized"],
                "task_success": facts["task_success"],
                "provider_invocations": facts["provider_invocations"],
            }
            execution["rows"].append(row)
            recount(execution)
            execution["checked_slots"] += int(facts["status"] == "checked")
            save(output / "execution.json", execution)
            sys.stdout.write(
                json.dumps(
                    {
                        "slot": index + 1,
                        "declared": len(protocol["queue"]),
                        "case_id": slot["case_id"],
                        "arm": arm.value,
                        "known_tokens": execution["known_tokens"],
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
            if (
                not unchanged
                or facts["actor_removed"] is not True
                or safety.get("active_credential_absent") is False
            ):
                execution["safety_passed"] = False
                execution["stop_reason"] = "critical_safety_violation"
                break
            if facts["status"] != "checked":
                execution["stop_reason"] = "candidate_unavailable"
                break
            if values["token_usage_complete"] is not True or values["finalized"] is not True:
                execution["stop_reason"] = "unknown_usage_or_unclosed_trace"
                break
        execution["status"] = (
            "completed" if len(execution["rows"]) == len(protocol["queue"]) else "stopped"
        )
    except asyncio.CancelledError:
        record_partial(execution, active, output)
        execution["status"], execution["stop_reason"] = "cancelled", "explicit_cancellation"
        raise
    except Exception:
        record_partial(execution, active, output)
        execution["status"], execution["stop_reason"] = "failed", "controller_or_adapter_failure"
        raise
    finally:
        recount(execution)
        complete_usage = all(r["usage_complete"] and r["finalized"] for r in execution["rows"])
        execution["actual_usage_complete"] = complete_usage
        execution["known_token_overrun"] = max(
            0, execution["known_tokens"] - protocol["total_token_budget"]
        )
        execution["token_overrun"] = execution["known_token_overrun"] if complete_usage else None
        execution["observed_token_budget_satisfied"] = (
            False if execution["known_token_overrun"] else (True if complete_usage else None)
        )
        save(output / "execution.json", execution)
    return execution


def study_comparison(
    slots: list[ComparisonRun], case_ids: list[str], *, safety_passed: bool | None, real: bool
) -> ComparisonReport:
    """Keep paired statistics; use P5's declared exposure opportunities for all arms."""
    comparison = compare_extension_groups(
        slots, case_ids=case_ids, safety_passed=safety_passed, real_model_results=real
    )
    return replace(
        comparison,
        groups={
            group: replace(
                summary,
                memory_model_exposure=replace(
                    summary.memory_model_exposure, eligible=summary.success_rate.eligible
                ),
                # P5 has no independent semantic-applicability labels.
                memory_selector_match=replace(summary.memory_selector_match, eligible=0),
            )
            for group, summary in comparison.groups.items()
        },
    )


def report(snapshot: Path, execution_root: Path, output: Path) -> dict[str, Any]:
    frozen, execution = check(snapshot, current=False), read(execution_root / "execution.json")
    if (
        execution["source_sha256"] != frozen["source_sha256"]
        or execution["protocol_sha256"] != frozen["protocol_sha256"]
    ):
        raise StudyError("execution_scope_changed")
    observed = {row["index"]: row for row in execution["rows"]}
    if len(observed) != len(execution["rows"]) or any(
        type(i) is not int or i not in range(len(frozen["protocol"]["queue"])) for i in observed
    ):
        raise StudyError("duplicate_or_unknown_execution_slot")
    slots, index = [], {}
    exposures = {}
    responses = 0
    served = {group: [] for group in ARM_GROUPS}
    for number, declared in enumerate(frozen["protocol"]["queue"]):
        entry = observed.get(number)
        if entry is None:
            slots.append(
                ComparisonRun(
                    declared["case_id"], declared["repeat"], declared["group"], observed=False
                )
            )
            continue
        if any(entry[k] != declared[k] for k in ("case_id", "repeat", "group")):
            raise StudyError("declared_slot_changed")
        expected_directory = "runs/" + ext.queue_id(number, declared)
        if entry["directory"] != expected_directory:
            raise StudyError("execution_directory_scope_changed")
        directory = execution_root / entry["directory"]
        if not (directory / "trace.jsonl").is_file():
            slots.append(
                ComparisonRun(
                    declared["case_id"],
                    declared["repeat"],
                    declared["group"],
                    tokens=entry["known_tokens"],
                    usage_complete=False,
                    finalized=False,
                )
            )
            continue
        trace = TraceRun.from_file(directory / "trace.jsonl")
        sidecar = (
            ScoringEvidence.model_validate_json((directory / "evidence.json").read_bytes())
            if (directory / "evidence.json").is_file()
            else None
        )
        started = trace.named("run_started")
        if (
            started is None
            or entry.get("run_id") not in {None, started.run_id}
            or (sidecar is not None and sidecar.case_id != declared["case_id"])
        ):
            raise StudyError("report_run_case_scope_mismatch")
        score = score_harness(trace, evidence=sidecar)
        measured = ext.comparison_run(declared, score, trace)
        witness_file = directory / "model_usage.json"
        audit = witness_metrics(
            directory,
            read(witness_file) if witness_file.is_file() else [],
            calls=entry["provider_invocations"],
        )
        calls = trace.by_name("model_call_started")
        bodies, cards = [], []
        for call_record in calls:
            messages = call_record.data.get("messages", [])
            bodies.append(
                any(m.get("role") == "user" and m.get("name") == "project_memory" for m in messages)
            )
            cards.append(
                any(
                    m.get("role") == "user" and m.get("name") == "project_memory_index"
                    for m in messages
                )
            )
        exposures[str(number)] = {
            "model_calls": len(calls),
            "body_exposed": any(bodies) if calls else None,
            "index_exposed": any(cards) if calls else None,
            "search_executions": sum(
                r.data.get("tool") == "search_memory" for r in trace.by_name("tool_call_started")
            ),
            "read_executions": sum(
                r.data.get("tool") == "read_memory" for r in trace.by_name("tool_call_started")
            ),
        }
        slots.append(
            replace(
                measured,
                tokens=max(measured.tokens or 0, entry["known_tokens"], audit["total_tokens"]),
                usage_complete=measured.usage_complete
                and entry["usage_complete"]
                and audit["token_usage_complete"],
                memory_exposed=exposures[str(number)]["body_exposed"],
                memory_match=None,
            )
        )
        responses += len(trace.by_name("model_call_completed"))
        served[declared["group"]].extend(r.data for r in trace.by_name("model_call_completed"))
        index[str(number)] = {
            "trace_sha256": pin(directory / "trace.jsonl"),
            "sidecar_sha256": pin(directory / "evidence.json")
            if (directory / "evidence.json").is_file()
            else None,
            "usage_witness_sha256": pin(witness_file) if witness_file.is_file() else None,
            "verifier_sha256": pin(directory / "verifier.json")
            if (directory / "verifier.json").exists()
            else None,
            "capture_sha256": pin(directory / "candidate.tar")
            if (directory / "candidate.tar").exists()
            else None,
            "isolation_receipts": {
                name: pin(directory / name) if (directory / name).is_file() else None
                for name in (
                    "actor_container.json",
                    "evaluator_container.json",
                    "check.json",
                    "p5_safety.json",
                )
            },
        }
    real = (
        execution["provider"] == "deepseek"
        and sum(r["provider_invocations"] for r in execution["rows"]) > 0
    )
    comparison = study_comparison(
        slots,
        case_ids=[
            t.task_id
            for t in BenchmarkManifest.from_file(ROOT / frozen["protocol"]["source_suite"]).tasks
        ],
        safety_passed=execution["safety_passed"],
        real=real,
    )
    result = {
        "schema_version": "1.0",
        "source_sha256": frozen["source_sha256"],
        "protocol_sha256": frozen["protocol_sha256"],
        "execution_sha256": pin(execution_root / "execution.json"),
        "group_legend": frozen["protocol"]["groups"],
        "declared_slots": len(frozen["protocol"]["queue"]),
        "checked_slots": execution["checked_slots"],
        "execution": execution,
        "comparisons": comparison.to_json(),
        "exposure": exposures,
        "evidence_index": index,
        "real_model_results": real,
        "real_provider_attempts": real,
        "successful_model_responses": responses,
        "served_model_metadata": {
            group: {
                "responses": len(records),
                "provider_models": sorted(
                    {str(r["provider_model"]) for r in records if r.get("provider_model")}
                ),
                "unknown_provider_models": sum(not r.get("provider_model") for r in records),
                "system_fingerprints": sorted(
                    {str(r["system_fingerprint"]) for r in records if r.get("system_fingerprint")}
                ),
                "unknown_system_fingerprints": sum(
                    not r.get("system_fingerprint") for r in records
                ),
            }
            for group, records in served.items()
        },
        "model_version_scope": "Configured model is fixed; server IDs/fingerprints are recorded "
        "when supplied and do not prove immutable weights.",
        "derivation_source_sha256": ext.digest(ext.canonical(source_inputs())),
        "derivation_note": "Read-only derivation from retained traces; not a new execution.",
        "benefit_evaluation": "assessable"
        if comparison.complete_cohort and real and execution["safety_passed"]
        else "not_assessable",
        "scope": (
            "Six fixed known tasks; paired exploratory results. "
            "Memory exposure is not semantic utility."
        ),
    }
    output.mkdir(parents=True, exist_ok=False)
    save(output / "report.json", result)
    (output / "report.md").write_text(
        "# Memory v2 P5 paired report\n\n"
        + json.dumps(
            {
                "source_sha256": frozen["source_sha256"],
                "group_legend": result["group_legend"],
                "checked_slots": result["checked_slots"],
                "declared_slots": result["declared_slots"],
                "real_model_results": real,
                "benefit_evaluation": result["benefit_evaluation"],
                "comparisons": comparison.to_json(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    return result


async def preflight_command(
    command: tuple[str, ...], workspace: Path, log: Path, *, command_limit: float = 300
) -> dict[str, Any]:
    runner = LocalCommandRunner(
        WorkspaceSandbox(workspace), CommandPolicy(allowed_commands=frozenset({"uv"}))
    )
    result = await runner.run(command, timeout_seconds=command_limit)
    await asyncio.to_thread(log.write_text, result.stdout + result.stderr)
    return {
        "command": command,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "log_sha256": pin(log),
    }


async def contract_probes(snapshot: Path, output: Path) -> dict[str, Any]:
    frozen = check(snapshot)
    matrix = read(ROOT / "docs/memory-v2-p5-matrix.json")
    collection = await preflight_command(
        ("uv", "run", "pytest", "--collect-only", "-q", *matrix["component_selection"]),
        ROOT,
        output / "contract-collection.log",
        command_limit=30,
    )
    nodes = re.findall(
        r"^tests/[^\n]+::[^\n]+$", (output / "contract-collection.log").read_text(), re.M
    )
    if collection["exit_code"] or not nodes or len(nodes) != len(set(nodes)):
        raise StudyError("contract_collection_failed")
    result = {
        "source_sha256": frozen["source_sha256"],
        "protocol_sha256": frozen["protocol_sha256"],
        "matrix_sha256": pin(ROOT / "docs/memory-v2-p5-matrix.json"),
        "collection": collection,
        "declared_nodes": nodes,
        "rows": [],
        "passed": False,
    }
    save(output / "contracts.json", result)
    for number, node in enumerate(nodes):
        log = output / f"contract-{number:03d}.log"
        receipt = await preflight_command(
            ("uv", "run", "pytest", "-q", node), ROOT, log, command_limit=30
        )
        text = log.read_text()
        passed = (
            receipt["exit_code"] == 0
            and receipt["timed_out"] is False
            and re.search(r"\b1 passed\b", text) is not None
            and "skipped" not in text
        )
        result["rows"].append({"node": node, **receipt, "status": "passed" if passed else "failed"})
        save(output / "contracts.json", result)
        if not passed:
            raise StudyError("preflight_contract_failed")
    result["passed"] = True
    save(output / "contracts.json", result)
    return result


async def reconstruction_probe(snapshot: Path, output: Path) -> dict[str, Any]:
    frozen = check(snapshot)
    with tempfile.TemporaryDirectory(prefix="minidsh-p5-rebuild-") as directory:
        root = Path(directory)
        # check() verified exact bounded regular members and their bytes; no extractall.
        with tarfile.open(snapshot / "source.tar.gz") as archive:
            for member in archive.getmembers():
                stream = archive.extractfile(member)
                if stream is None:
                    raise StudyError("source_reconstruction_failed")
                ext.write_private(root / member.name, stream.read())
        if ext.source_inputs(root) != frozen["source_files"]:
            raise StudyError("source_reconstruction_changed")
        result = await preflight_command(
            ("uv", "run", "--frozen", "--offline", "pytest", "-q", "tests/test_memory_study.py"),
            root,
            output / "reconstruction.log",
        )
    result.update(
        source_sha256=frozen["source_sha256"],
        source_archive_sha256=frozen["source_archive_sha256"],
        source_files=len(frozen["source_files"]),
        scope="Fresh temporary source reconstruction, frozen/offline install and arm contracts; "
        "not another full-suite or real-model execution",
    )
    save(output / "reconstruction.json", result)
    if result["exit_code"] or result["timed_out"]:
        raise StudyError("source_reconstruction_check_failed")
    return result


async def preflight_run(snapshot: Path, bundle: Path, output: Path) -> dict[str, Any]:
    frozen, _, _ = await checked_seed(snapshot, bundle, real=False)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False, mode=0o700)
    gates = []
    commands = (
        ("uv", "run", "ruff", "format", "--check", "."),
        ("uv", "run", "ruff", "check", "."),
        ("uv", "run", "mypy", "src", "tests"),
        ("uv", "run", "pytest"),
    )
    for number, command in enumerate(commands):
        gates.append(await preflight_command(command, ROOT, output / f"gate-{number}.log"))
        save(output / "gates.json", gates)
        if gates[-1]["exit_code"] or gates[-1]["timed_out"]:
            raise StudyError("preflight_quality_gate_failed")
    await contract_probes(snapshot, output)
    await reconstruction_probe(snapshot, output)
    api, probes = SimpleNamespace(**globals()), probe_module()
    await probes.mechanism_probes(api, snapshot, bundle, output / "native/mechanisms")
    await probes.os_probes(api, snapshot, output / "native/os")
    native_sha256 = check_native_probes(frozen, output)
    execution = await run(snapshot, bundle, output / "offline", provider="scripted")
    derived = report(snapshot, output / "offline", output / "offline_report")
    expected = len(frozen["protocol"]["queue"])
    proof = {
        "schema_version": "1.0",
        "source_sha256": frozen["source_sha256"],
        "protocol_sha256": frozen["protocol_sha256"],
        "passed": execution["checked_slots"] == expected
        and execution["safety_passed"] is True
        and all(g["exit_code"] == 0 for g in gates),
        "offline_checked_slots": execution["checked_slots"],
        "native_sha256": native_sha256,
        "contracts_sha256": pin(output / "contracts.json"),
        "reconstruction_sha256": pin(output / "reconstruction.json"),
        "real_model_calls": 0,
        "phase_acceptance": False,
        "offline_benefit_evaluation": derived["benefit_evaluation"],
        "note": (
            "Scripted data prove execution/acceptance/denominators only; not real model utility."
        ),
        "files": files_in(output, excluding=frozenset({"preflight.json"})),
    }
    save(output / "preflight.json", proof)
    if proof["passed"] is not True:
        raise StudyError("preflight_cohort_failed")
    return proof


def main() -> int:  # noqa: PLR0915 - explicit study subcommands
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freezing = commands.add_parser("plan")
    freezing.add_argument("--output", type=Path, required=True)
    checking = commands.add_parser("check")
    checking.add_argument("--snapshot", type=Path, required=True)
    preparing = commands.add_parser("prepare")
    preparing.add_argument("--snapshot", type=Path, required=True)
    preparing.add_argument("--source-seed", type=Path, required=True)
    preparing.add_argument("--output", type=Path, required=True)
    preparing.add_argument("--provider", choices=("scripted", "deepseek"), default="scripted")
    preparing.add_argument("--credentials", type=Path)
    preparing.add_argument("--preflight", type=Path)
    approval = commands.add_parser("authorize")
    approval.add_argument("--snapshot", type=Path, required=True)
    approval.add_argument("--bundle", type=Path, required=True)
    approval.add_argument("--authorization-file", type=Path, required=True)
    approval.add_argument("--output", type=Path, required=True)
    approval.add_argument("--fixture", action="store_true")
    running = commands.add_parser("run")
    running.add_argument("--snapshot", type=Path, required=True)
    running.add_argument("--bundle", type=Path, required=True)
    running.add_argument("--output", type=Path, required=True)
    running.add_argument("--provider", choices=("scripted", "deepseek"), default="scripted")
    running.add_argument("--credentials", type=Path)
    running.add_argument("--preflight", type=Path)
    probing = commands.add_parser("preflight")
    probing.add_argument("--snapshot", type=Path, required=True)
    probing.add_argument("--bundle", type=Path, required=True)
    probing.add_argument("--output", type=Path, required=True)
    deriving = commands.add_parser("report")
    deriving.add_argument("--snapshot", type=Path, required=True)
    deriving.add_argument("--execution", type=Path, required=True)
    deriving.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for key in (
        "snapshot",
        "output",
        "source_seed",
        "bundle",
        "authorization_file",
        "execution",
        "preflight",
        "credentials",
    ):
        value = getattr(args, key, None)
        if value is not None:
            setattr(args, key, value.absolute())
    try:
        if args.command == "plan":
            result = plan(args.output)
        elif args.command == "check":
            result = check(args.snapshot)
        elif args.command == "prepare":
            result = asyncio.run(
                prepare(
                    args.snapshot,
                    args.source_seed,
                    args.output,
                    provider=args.provider,
                    credentials=args.credentials,
                    preflight=args.preflight,
                )
            )
        elif args.command == "authorize":
            result = asyncio.run(
                authorize(
                    args.snapshot,
                    args.bundle,
                    args.authorization_file,
                    args.output,
                    fixture=args.fixture,
                )
            )
        elif args.command == "run":
            result = asyncio.run(
                run(
                    args.snapshot,
                    args.bundle,
                    args.output,
                    provider=args.provider,
                    credentials=args.credentials,
                    preflight=args.preflight,
                )
            )
        elif args.command == "preflight":
            result = asyncio.run(preflight_run(args.snapshot, args.bundle, args.output))
        else:
            result = report(args.snapshot, args.execution, args.output)
        sys.stdout.write(
            json.dumps(
                {
                    "status": "ok",
                    "source_sha256": result.get("source_sha256"),
                    "protocol_sha256": result.get("protocol_sha256"),
                }
            )
            + "\n"
        )
        return 0
    except (StudyError, ext.AcceptanceError) as exc:
        sys.stderr.write("memory-study: " + str(exc) + "\n")
    except Exception as exc:
        sys.stderr.write(
            "memory-study: invalid_input_or_execution_failure [" + type(exc).__name__ + "]\n"
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
