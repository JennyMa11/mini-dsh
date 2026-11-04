"""Freeze, check, execute and derive Extension v1 acceptance evidence.

Generated packages contain explicitly allowed source/fixture files, never provider
credentials. Existing packages and execution directories are never overwritten.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from minidsh.adapters import JsonlTraceWriter, ScriptedModel
from minidsh.adapters.file_backend import DockerFileBackend
from minidsh.adapters.memory import SQLiteMemoryStore
from minidsh.adapters.memory_evidence import JsonMemoryEvidenceSource
from minidsh.adapters.models import CredentialRedactingModel, OpenAICompatibleModel
from minidsh.benchmark.extension_matrix import ExtensionGroup, build_extension_runtime
from minidsh.benchmark.hashes import frozen_tree_digest
from minidsh.benchmark.public_lite import checked_image_size, seed_container
from minidsh.benchmark.runner import BenchmarkManifest, BenchmarkRunner
from minidsh.composition import (
    RuntimeComponents,
    RuntimeEnvironment,
    RuntimeOptions,
    build_local_runtime,
)
from minidsh.domain import AgentBudget
from minidsh.domain.extensions import FinishDecision
from minidsh.domain.memory import MemoryConfig, MemoryKind, MemoryStatus, MemoryValidationError
from minidsh.evaluation.extension_comparison import (
    ComparisonRun,
    MetricComparison,
    compare_extension_groups,
)
from minidsh.evaluation.harness_metrics import (
    HarnessMetrics,
    MetricRate,
    ScoringEvidence,
    aggregate_harness_metrics,
    score_harness,
)
from minidsh.evaluation.tracefile import TraceError, TraceRun
from minidsh.infrastructure import CommandPolicy
from minidsh.infrastructure.containers import ContainerResources, DockerHost, DockerSandbox
from minidsh.ports.memory import MemoryEvidenceError, MemoryStoreError
from minidsh.runtime.hooks import HookRegistry

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "benchmarks/extension_v1/manifest.json"
REAL_RUNS = 90
TOKEN_BUDGET = 720_000
MEMORY_MB = 1024
NANO_CPUS = 1_000_000_000
PIDS_LIMIT = 128
SEED_COUNT = 3
_ROOTS = ("src", "tests", "scripts", "examples", "benchmarks", "docs", ".github")
_SUFFIXES = frozenset(
    {".py", ".js", ".html", ".css", ".json", ".md", ".txt", ".toml", ".yml", ".yaml", ".sh", ".svg"}
)
_FILES = (
    "pyproject.toml",
    "uv.lock",
    "AGENTS.md",
    "README.md",
    "CHANGELOG.md",
    "GLOSSARY.md",
    "LICENSE",
    ".gitignore",
    ".python-version",
    "benchmarks/stress/fixtures/stress-large-output/big.log",
)


class AcceptanceError(RuntimeError):
    """A safe diagnostic without private configuration or provider text."""


class _SeedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class _SeedBinding(_SeedRecord):
    entry_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class _Pinned(_SeedRecord):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class _SeedProfile(_SeedRecord):
    schema_version: Literal["1.0"]
    memory: MemoryConfig
    database_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval: _Pinned
    entries: list[_SeedBinding] = Field(min_length=SEED_COUNT, max_length=SEED_COUNT)


class _SeedApproval(_SeedRecord):
    schema_version: Literal["1.0"]
    purpose: Literal["extension_v1_frozen_queue"]
    project_id: str
    applicable_version: str
    approved_by: str = Field(min_length=1)
    approved_at: str = Field(min_length=1)
    review_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    authorization_kind: Literal["synthetic_fixture", "explicit_user_approval"] = "synthetic_fixture"
    protocol_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    entries: list[_SeedBinding] = Field(min_length=SEED_COUNT, max_length=SEED_COUNT)


def read_seed_file(path: Path, expected: str | None = None) -> bytes:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise AcceptanceError("memory_seed_denied")
    info = path.stat()
    if info.st_nlink != 1 or info.st_size > 8 * 1024 * 1024:
        raise AcceptanceError("memory_seed_denied")
    with path.open("rb") as stream:
        data = stream.read(8 * 1024 * 1024 + 1)
    if len(data) != info.st_size or (expected is not None and digest(data) != expected):
        raise AcceptanceError("memory_seed_changed")
    return data


async def load_seed(
    path: Path, *, workspace: Path, frozen_snapshot: Path | None = None
) -> _SeedProfile:
    """Verify durable rows and independently backed provenance before provider access.

    The host supplies this approval receipt; no model argument or model-visible
    file can authorize a seed. Synthetic fixtures test this boundary separately.
    """
    try:
        profile = _SeedProfile.model_validate_json(await asyncio.to_thread(read_seed_file, path))
        if frozen_snapshot is not None:
            profile = profile.model_copy(
                update={
                    "memory": replace(
                        profile.memory,
                        database=str(frozen_snapshot / "memory_seed.sqlite3"),
                        storage_root=str(frozen_snapshot),
                        evidence_root=str(frozen_snapshot / "memory_evidence"),
                    ),
                    "approval": _Pinned(
                        path=str(frozen_snapshot / "memory_approval.json"),
                        sha256=profile.approval.sha256,
                    ),
                }
            )
        config = profile.memory
        approval = _SeedApproval.model_validate_json(
            await asyncio.to_thread(
                read_seed_file, Path(profile.approval.path), profile.approval.sha256
            )
        )
        expected = {entry.entry_id: entry for entry in profile.entries}
        authorized = {entry.entry_id: entry for entry in approval.entries}
        if (
            len(expected) != SEED_COUNT
            or expected != authorized
            or approval.project_id != config.project_id
            or approval.applicable_version != config.applicable_version
            or config.evidence_root is None
            or not approval.approved_by.strip()
        ):
            raise AcceptanceError("memory_seed_approval_mismatch")
        await asyncio.to_thread(read_seed_file, Path(config.database), profile.database_sha256)
        store = SQLiteMemoryStore(
            Path(config.database), allowed_root=Path(config.storage_root), workspace=workspace
        )
        try:
            rows = await store.snapshot(config.project_id)
        finally:
            await store.close()
        if len(rows) != SEED_COUNT or {row.entry_id for row in rows} != set(expected):
            raise AcceptanceError("memory_seed_entries_mismatch")
        if sorted(row.kind.value for row in rows) != sorted(
            [
                MemoryKind.ENVIRONMENT.value,
                MemoryKind.REPAIR.value,
                MemoryKind.REPAIR.value,
            ]
        ):
            raise AcceptanceError("memory_seed_origins_mismatch")
        reader = JsonMemoryEvidenceSource(
            Path(config.evidence_root), workspace=workspace, grants=config.evidence_grants
        )
        for row in rows:
            binding = expected[row.entry_id]
            if (
                row.status is not MemoryStatus.COMMITTED
                or row.revision != binding.revision
                or row.content_sha256 != binding.content_sha256
                or row.applicable_version != config.applicable_version
                or row.approved_by != approval.approved_by
                or row.approved_at != approval.approved_at
                or row.evidence_sha256 is None
            ):
                raise AcceptanceError("memory_seed_approval_mismatch")
            proof = await reader.resolve_sha256(row.evidence_sha256)
            proposal = replace(
                row,
                status=MemoryStatus.PROPOSED,
                validation_sha256=None,
                approved_by=None,
                approved_at=None,
                approved_revision=None,
                approval_sha256=None,
            )
            if proposal.validate(proof).proposal_sha256 != row.proposal_sha256:
                raise AcceptanceError("memory_seed_evidence_mismatch")
        await asyncio.to_thread(read_seed_file, Path(config.database), profile.database_sha256)
        return profile
    except (ValidationError, MemoryStoreError, MemoryEvidenceError, MemoryValidationError):
        raise AcceptanceError("memory_seed_invalid") from None


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


def freeze_seed(path: Path, output: Path) -> dict[str, Any]:
    raw = read_seed_file(path)
    profile = asyncio.run(load_seed(path, workspace=output))
    raw = read_seed_file(path, digest(raw))
    files: dict[str, str] = {}

    def save(name: str, data: bytes) -> None:
        write_private(output / name, data)
        files[name] = digest(data)

    save("memory_seed.json", raw)
    save(
        "memory_seed.sqlite3",
        read_seed_file(Path(profile.memory.database), profile.database_sha256),
    )
    save(
        "memory_approval.json", read_seed_file(Path(profile.approval.path), profile.approval.sha256)
    )
    root = Path(profile.memory.evidence_root or "")
    for grant in profile.memory.evidence_grants:
        raw_document = read_seed_file(root / grant.document.path, grant.document.sha256)
        document = json.loads(raw_document)
        refs = (
            [document["configuration"]]
            if document["kind"] == "operator_configuration"
            else [
                document["acceptance"],
                document["trace"],
                document["criterion"],
                *document["artifacts"],
            ]
        )
        refs.append(asdict(grant.document))
        for reference in refs:
            name = "memory_evidence/" + reference["path"]
            data = read_seed_file(root / reference["path"], reference["sha256"])
            if name in files:
                if files[name] != digest(data):
                    raise AcceptanceError("memory_seed_changed")
            else:
                save(name, data)
    (output / "seed_audit_workspace").mkdir(mode=0o700)
    return {"manifest_path": str(path), "manifest_sha256": digest(raw), "files": files}


def source_inputs(root: Path) -> dict[str, str]:
    files: set[Path] = set()
    for name in _ROOTS:
        for path in (root / name).rglob("*"):
            if path.suffix not in _SUFFIXES or "__pycache__" in path.parts:
                continue
            if (
                path.parent == root / "docs/results"
                and path.name.startswith(("extension-pr4-", "extension-m5-"))
                and path.suffix in {".json", ".md"}
            ):
                continue
            if path.is_symlink():
                raise AcceptanceError("source_symlink_denied")
            if path.is_file():
                files.add(path)
    files.update(root / name for name in _FILES if (root / name).is_file())
    return {path.relative_to(root).as_posix(): digest(path.read_bytes()) for path in sorted(files)}


def check_protocol(protocol: dict[str, Any], root: Path) -> None:
    if (
        protocol["plan_version"] != "v1"
        or protocol["real_model_runs"] != REAL_RUNS
        or len(protocol["queue"]) != REAL_RUNS
        or protocol["total_token_budget"] != TOKEN_BUDGET
    ):
        raise AcceptanceError("protocol_scope_changed")
    for name, expected in protocol["input_hashes"].items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise AcceptanceError("input_path_denied")
        if digest((root / "benchmarks/extension_v1" / name).read_bytes()) != expected:
            raise AcceptanceError("frozen_input_changed")
    isolation = protocol["isolation"]
    if (
        isolation["network"] != "none"
        or isolation["cpus"] != 1
        or isolation["memory_mb"] != MEMORY_MB
    ):
        raise AcceptanceError("isolation_scope_changed")
    tasks = BenchmarkManifest.from_file(root / "benchmarks/extension_v1/offline.json").tasks
    for task in tasks:
        if frozen_tree_digest(task.workspace) != task.repo_commit or (
            task.verification_overlay is None
            or frozen_tree_digest(task.verification_overlay) != task.verification_commit
        ):
            raise AcceptanceError("frozen_task_tree_changed")
    for name in ("bug_fix", "test_debug", "code_review"):
        path = root / "examples/skills" / name / "SKILL.md"
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
            raise AcceptanceError("required_skill_source_missing")


def freeze(output: Path, seed_manifest: Path | None = None) -> None:
    protocol = json.loads((ROOT / PROTOCOL).read_text())
    check_protocol(protocol, ROOT)
    files = source_inputs(ROOT)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    archive = output / "source.tar.gz"
    with (
        archive.open("xb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        for name in files:
            data = (ROOT / name).read_bytes()
            if digest(data) != files[name]:
                raise AcceptanceError("source_changed_during_freeze")
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    manifest = {
        "schema_version": "1.0",
        "protocol": protocol,
        "protocol_sha256": digest(canonical(protocol)),
        "source_files": files,
        "source_sha256": digest(canonical(files)),
        "source_archive_sha256": digest(archive.read_bytes()),
        "allowlist": {"roots": _ROOTS, "suffixes": sorted(_SUFFIXES), "files": _FILES},
        "excluded": [
            ".env*",
            "credentials",
            "caches",
            "raw traces",
            "working copies",
            "docs/results/extension-{pr4,m5}-*.{json,md} derived outputs",
        ],
        "real_model_runs_completed": 0,
        "memory_seed": freeze_seed(seed_manifest, output) if seed_manifest is not None else None,
    }
    write_json(output / "manifest.json", manifest)


def check(snapshot: Path, *, current: bool = True) -> dict[str, Any]:
    snapshot = snapshot.resolve()
    manifest: dict[str, Any] = json.loads((snapshot / "manifest.json").read_text())
    files = manifest["source_files"]
    if digest(canonical(files)) != manifest["source_sha256"]:
        raise AcceptanceError("source_manifest_changed")
    if digest(canonical(manifest["protocol"])) != manifest["protocol_sha256"]:
        raise AcceptanceError("protocol_manifest_changed")
    archive = snapshot / "source.tar.gz"
    if digest(archive.read_bytes()) != manifest["source_archive_sha256"]:
        raise AcceptanceError("source_archive_changed")
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) != len(files) or {m.name for m in members} != set(files):
            raise AcceptanceError("source_archive_members_changed")
        for member in members:
            path = PurePosixPath(member.name)
            if not member.isfile() or path.is_absolute() or ".." in path.parts:
                raise AcceptanceError("source_archive_path_denied")
            stream = tar.extractfile(member)
            data = stream.read() if stream is not None else b""
            if stream is None or digest(data) != files[member.name]:
                raise AcceptanceError("source_archive_member_changed")
            if member.name == PROTOCOL and json.loads(data) != manifest["protocol"]:
                raise AcceptanceError("protocol_archive_mismatch")
    if current:
        if source_inputs(ROOT) != files:
            raise AcceptanceError("current_source_changed")
        check_protocol(manifest["protocol"], ROOT)
    seed = manifest.get("memory_seed")
    if seed is not None:
        check_frozen_seed(snapshot, seed, current=current)
    return manifest


def check_frozen_seed(snapshot: Path, seed: dict[str, Any], *, current: bool) -> None:
    for name, expected in seed["files"].items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise AcceptanceError("memory_seed_denied")
        read_seed_file(snapshot / name, expected)
    read_seed_file(snapshot / "memory_seed.json", seed["manifest_sha256"])
    asyncio.run(
        load_seed(
            snapshot / "memory_seed.json",
            workspace=snapshot / "seed_audit_workspace",
            frozen_snapshot=snapshot,
        )
    )
    if current:
        path = Path(seed["manifest_path"])
        read_seed_file(path, seed["manifest_sha256"])
        asyncio.run(load_seed(path, workspace=snapshot))


def check_phase(path: Path, frozen: dict[str, Any]) -> None:
    """Require the source/seed-bound full matrix and all indexed evidence bytes."""
    seed = frozen.get("memory_seed")
    if seed is None:
        raise AcceptanceError("approved_frozen_seeds_required")
    path = path.resolve()
    seal = json.loads(read_seed_file(path))
    if (
        seal.get("status") != "accepted_pr4_only"
        or seal.get("source_sha256") != frozen["source_sha256"]
        or seal.get("memory_seed_manifest_sha256") != seed["manifest_sha256"]
    ):
        raise AcceptanceError("pr4_acceptance_scope_mismatch")
    root = path.parent
    index = json.loads(read_seed_file(root / "evidence_index.json", seal["evidence_index_sha256"]))
    for name, expected in index["files"].items():
        relative = PurePosixPath(name)
        candidate = root / name
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or candidate.is_symlink()
            or not candidate.resolve(strict=True).is_relative_to(root)
            or candidate.stat().st_nlink != 1
        ):
            raise AcceptanceError("pr4_acceptance_evidence_denied")
        with candidate.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise AcceptanceError("pr4_acceptance_evidence_changed")
    if "coverage.json" not in index["files"]:
        raise AcceptanceError("pr4_acceptance_not_complete")
    coverage = json.loads((root / "coverage.json").read_text())
    required = {case["case_id"] for case in frozen["protocol"]["cases"]}
    rows = coverage["rows"]
    if (
        coverage["source_sha256"] != frozen["source_sha256"]
        or len(rows) != len(required)
        or {row["case_id"] for row in rows} != required
        or any(row["status"] != "pass" or row["evidence"] not in index["files"] for row in rows)
    ):
        raise AcceptanceError("pr4_acceptance_not_complete")


def queue_id(index: int, row: dict[str, Any]) -> str:
    return f"{index:03d}-{row['case_id']}-{row['repeat']}-{row['group']}"


def audit_hooks() -> HookRegistry:
    async def observe(_value: Any) -> None:
        pass

    async def model(request: Any, _view: Any) -> Any:
        return request

    async def tool(call: Any, _view: Any) -> Any:
        return call

    async def after_tool(_observation: Any, _view: Any) -> None:
        pass

    async def finish(_proposal: Any) -> FinishDecision:
        return FinishDecision()

    hooks = HookRegistry()
    hooks.register_v2(
        "acceptance_audit",
        "1.0",
        on_run_start=observe,
        before_model=model,
        before_tool=tool,
        after_tool=after_tool,
        on_step_end=observe,
        before_finish=finish,
        on_run_end=observe,
    )
    return hooks


async def run_offline(snapshot: Path, output: Path) -> None:  # noqa: PLR0915 - durable sequential receipts
    """Reuse the existing six-task BenchmarkRunner, once per compatibility arm.

    This local deterministic replay never executes model-generated commands. It
    is deliberately separate from the mount-free, real-provider M5 queue.
    """
    frozen = await asyncio.to_thread(check, snapshot)
    tasks = {
        task.task_id: task
        for task in BenchmarkManifest.from_file(ROOT / "benchmarks/extension_v1/offline.json").tasks
    }
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    execution = {
        "schema_version": "1.0",
        "provider": "scripted",
        "mode": "offline_compatibility",
        "source_sha256": frozen["source_sha256"],
        "snapshot_manifest_sha256": digest((snapshot / "manifest.json").read_bytes()),
        "real_model_api_calls": 0,
        "attempted_rows": 0,
        "status": "running",
        "scope": "six frozen tasks, Baseline/A, repeat zero; not the real-model cohort",
    }
    write_json(output / "execution.json", execution)
    stable: dict[str, dict[str, Any]] = {}
    accepted = True
    try:
        for index, declared in enumerate(frozen["protocol"]["queue"]):
            if declared["repeat"] != 0 or declared["group"] not in {"Baseline", "A"}:
                continue
            directory = output / "runs" / queue_id(index, declared)
            directory.mkdir(parents=True, exist_ok=False)
            task = tasks[declared["case_id"]]
            runtime_holder = []
            model_holder = []

            def factory(  # noqa: PLR0913 - bind per-row state before the next iteration
                workspace: Path,
                task: Any,
                *,
                group: str = declared["group"],
                artifacts: Path = directory / "artifacts",
                holder: list[Any] = runtime_holder,
                models: list[Any] = model_holder,
            ) -> Any:
                if task.response_script is None:
                    raise AcceptanceError("offline_script_missing")
                model = ScriptedModel.from_file(task.response_script)
                models.append(model)
                runtime = build_local_runtime(
                    workspace=workspace,
                    model=model,
                    options=RuntimeOptions(
                        allow_write=True,
                        allow_shell=True,
                        allowed_commands=frozenset(task.isolation.allowed_commands),
                        test_command=task.test_argv,
                    ),
                    budget=task.budget,
                    components=RuntimeComponents(hooks=audit_hooks() if group == "A" else None),
                )

                async def capture(_event: Any) -> None:
                    shutil.copytree(
                        workspace,
                        artifacts,
                        ignore=shutil.ignore_patterns("__pycache__", ".minidsh"),
                        symlinks=True,
                    )

                runtime.events.subscribe("run_finished", capture)
                holder.append(runtime)
                return runtime

            execution["attempted_rows"] += 1
            write_json(output / "execution.json", execution)
            try:
                measured = await BenchmarkRunner(
                    factory,
                    directory / "runner",
                    verification_allowlist=frozenset({"python", "node"}),
                ).run_one(task)
            finally:
                for runtime in runtime_holder:
                    if runtime.close is not None:
                        await runtime.close()
            shutil.copyfile(Path(measured.trace_path), directory / "trace.jsonl")
            trace = TraceRun.from_file(directory / "trace.jsonl")
            requests = [asdict(request) for request in model_holder[0].requests]
            write_json(directory / "requests.json", requests)
            run_id = trace.records[0].run_id
            sidecar = ScoringEvidence(
                run_id=run_id,
                case_id=task.task_id,
                source=f"BenchmarkRunner independent overlay {task.verification_commit}",
                task_success=measured.success,
            )
            (directory / "evidence.json").write_text(sidecar.model_dump_json(indent=2) + "\n")
            write_json(
                directory / "check.json",
                {
                    "run_id": run_id,
                    "case_id": task.task_id,
                    "criterion_sha256": task.verification_commit,
                    "verifier_exit_code": measured.verifier_exit_code,
                    "task_success": measured.success,
                    "trace_sha256": digest((directory / "trace.jsonl").read_bytes()),
                    "evidence_sha256": digest((directory / "evidence.json").read_bytes()),
                    "status": "pass" if measured.success else "fail",
                },
            )
            accepted &= measured.success
            stable.setdefault(task.task_id, {})[declared["group"]] = {
                "requests": requests,
                "actions": [
                    {key: record.data.get(key) for key in ("call_id", "tool", "arguments")}
                    for record in trace.by_name("tool_call_started")
                ],
                "status": measured.status,
                "success": measured.success,
                "steps": measured.steps,
                "tool_calls": measured.tool_calls,
                "input_tokens": measured.input_tokens,
                "output_tokens": measured.output_tokens,
                "cached_input_tokens": measured.cached_input_tokens,
                "artifacts": {
                    path.relative_to(directory / "artifacts").as_posix(): digest(path.read_bytes())
                    for path in sorted((directory / "artifacts").rglob("*"))
                    if path.is_file()
                },
            }
        cases = {
            case_id: pair.get("Baseline") == pair.get("A") and set(pair) == {"Baseline", "A"}
            for case_id, pair in stable.items()
        }
        execution["compatibility"] = {"all_equal": all(cases.values()), "cases": cases}
        write_json(output / "compatibility.json", stable)
        if not accepted or not all(cases.values()):
            execution["status"] = "failed"
            raise AcceptanceError("offline_criterion_or_compatibility_failed")
        execution["status"] = "completed"
    except BaseException:
        if execution["status"] == "running":
            execution["status"] = "interrupted"
        raise
    finally:
        write_json(output / "execution.json", execution)


class ObservedModel:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.requests: list[Any] = []
        self.calls = 0

    async def complete(self, request: Any) -> Any:
        self.calls += 1
        self.requests.append(request)
        return await self.inner.complete(request)


def provider_model(protocol: dict[str, Any], credentials: Path | None) -> Any:
    secret = os.environ.get("DEEPSEEK_API_KEY", "")
    if credentials is not None:
        for line in credentials.read_text().splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "DEEPSEEK_API_KEY":
                secret = value.strip().strip("\"'")
    if not secret:
        raise AcceptanceError("deepseek_credentials_required")
    config = protocol["model"]
    return CredentialRedactingModel(
        OpenAICompatibleModel(
            endpoint=config["endpoint"],
            model=config["id"],
            api_key=secret,
            temperature=config["temperature"],
            thinking=config["thinking"],
            max_attempts=1,
            max_output_tokens=config["max_output_tokens"],
            require_usage=True,
        ),
        secret,
    )


def archive_hashes(path: Path) -> dict[str, Any]:
    """Inspect tar bytes only. Never materialize actor-controlled links on the host."""
    result = {}
    total = 0
    with tarfile.open(path, "r:") as archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise AcceptanceError("candidate_archive_path_denied")
            if member.isfile():
                total += member.size
                if total > 8 * 1024 * 1024:
                    raise AcceptanceError("candidate_logical_size_limit")
                stream = archive.extractfile(member)
                if stream is None:
                    raise AcceptanceError("candidate_archive_invalid")
                result[str(relative)] = {"kind": "file", "sha256": digest(stream.read())}
            elif member.issym() or member.islnk():
                result[str(relative)] = {
                    "kind": "symlink" if member.issym() else "hardlink",
                    "target": member.linkname,
                }
            elif not member.isdir():
                result[str(relative)] = {"kind": "special", "tar_type": member.type.hex()}
    return result


# This fixed trusted program runs only after the actor container is gone, in a
# fresh evaluator. It installs criteria outside the candidate's path namespace.
_GRADE_PY = """import os,pathlib,shutil
source=pathlib.Path('/acceptance'); target=pathlib.Path('/app')
target.chmod(0o700)
for item in source.rglob('*'):
 if not item.is_file(): continue
 relative=item.relative_to(source); parent=target
 for part in relative.parts[:-1]:
  parent=parent/part
  if parent.is_symlink() or (parent.exists() and not parent.is_dir()): parent.unlink()
  parent.mkdir(exist_ok=True)
  parent.chmod(0o700)
 destination=target/relative
 if destination.is_symlink() or destination.is_file(): destination.unlink()
 elif destination.exists(): shutil.rmtree(destination)
 shutil.copyfile(item,destination)
"""
_GRADE_JS = """const fs=require('node:fs'),path=require('node:path');
fs.chmodSync('/app',0o700);
function install(source,target){
 for(const name of fs.readdirSync(source)){
  const input=path.join(source,name),output=path.join(target,name),info=fs.lstatSync(input);
  if(info.isDirectory()){
   try{if(!fs.lstatSync(output).isDirectory()||fs.lstatSync(output).isSymbolicLink())
    fs.rmSync(output,{recursive:true,force:true});}
   catch(e){if(e.code!=='ENOENT')throw e;}
   fs.mkdirSync(output,{recursive:true});fs.chmodSync(output,0o700);install(input,output);
  }else{fs.rmSync(output,{recursive:true,force:true});fs.copyFileSync(input,output);}
 }
}
install('/acceptance','/app');"""


async def record_world(host: DockerHost, box: DockerSandbox, destination: Path) -> None:
    inspected = await host.run(("docker", "inspect", box.name))
    if inspected.exit_code:
        raise AcceptanceError("container_inspection_failed")
    info = json.loads(inspected.stdout)[0]
    config = info["HostConfig"]
    receipt = {
        "name": box.name,
        "image": info["Config"]["Image"],
        "mounts": info["Mounts"],
        "network": config["NetworkMode"],
        "memory": config["Memory"],
        "nano_cpus": config["NanoCpus"],
        "pids_limit": config["PidsLimit"],
        "cap_drop": config["CapDrop"],
        "security_opt": config["SecurityOpt"],
    }
    write_json(destination, receipt)
    if (
        receipt["mounts"]
        or receipt["network"] != "none"
        or receipt["memory"] != MEMORY_MB * 1024 * 1024
        or receipt["nano_cpus"] != NANO_CPUS
        or receipt["pids_limit"] != PIDS_LIMIT
        or "ALL" not in receipt["cap_drop"]
        or not any("no-new-privileges" in item for item in receipt["security_opt"])
    ):
        raise AcceptanceError("container_isolation_mismatch")


async def isolated_row(  # noqa: PLR0912, PLR0913, PLR0915, PLR0917 - actor then independent evaluator
    snapshot: Path,
    directory: Path,
    declared: dict[str, Any],
    task: Any,
    protocol: dict[str, Any],
    host: DockerHost,
    model: ObservedModel,
    remaining: int,
) -> dict[str, Any]:
    group = ExtensionGroup(declared["group"])
    language = "js" if task.test_argv[0] == "node" else "python"
    image = protocol["isolation"]["images"]["images"][language]
    resources = ContainerResources(
        platform=protocol["isolation"]["platform"], cpus=1, memory_mb=1024, pids_limit=128
    )
    actor_input, workspace = directory / "input", directory / "actor_workspace"
    await asyncio.to_thread(
        shutil.copytree,
        task.workspace,
        actor_input,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    # The same auxiliary corpus is present in every arm; only its capability and
    # controlled context exposure change. It is not copied from an answer overlay.
    for name in ("bug_fix", "test_debug", "code_review"):
        target = actor_input / "skills" / name / "SKILL.md"
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(
            shutil.copyfile, ROOT / "examples/skills" / name / "SKILL.md", target
        )
    await asyncio.to_thread(workspace.mkdir, mode=0o700)
    memory = None
    if group is ExtensionGroup.D:
        profile = await load_seed(
            snapshot / "memory_seed.json", workspace=workspace, frozen_snapshot=snapshot
        )
        private = directory / "private"
        await asyncio.to_thread(private.mkdir, mode=0o700)
        data = await asyncio.to_thread(
            read_seed_file, Path(profile.memory.database), profile.database_sha256
        )
        await asyncio.to_thread(write_private, private / "memory.sqlite3", data)
        memory = replace(
            profile.memory, database=str(private / "memory.sqlite3"), storage_root=str(private)
        )
    limits = dict(protocol["budget"])
    limits["max_tokens"] = min(limits["max_tokens"], remaining)
    budget = AgentBudget(**limits)
    write_json(
        directory / "configuration.json",
        {
            "group": group.value,
            "budget": limits,
            "visible_argv": task.test_argv,
            "image": image,
            "network": "none",
            "memory": memory is not None,
            "test_timeout_seconds": 30,
            "independent_timeout_seconds": 10,
            "workspace_capture": "bounded raw archive; extraction only in independent container",
            "python_cache_policy": "retain raw archive; independent grade ignores bytecode caches",
        },
    )
    run_id = uuid.uuid4().hex
    archive = directory / "candidate.tar"
    captured = False
    async with DockerSandbox(host, image=image, resources=resources) as actor:
        await record_world(host, actor, directory / "actor_container.json")
        await seed_container(host, actor, actor_input)
        interpreter = "node" if language == "js" else "python3"
        setup = (
            ("node", "-e", "require('node:fs').symlinkSync('/app','/workspace','dir')")
            if language == "js"
            else (
                "python3",
                "-c",
                "from pathlib import Path; "
                "Path('/workspace').symlink_to('/app',target_is_directory=True)",
            )
        )
        if (await actor.execute(setup)).exit_code:
            raise AcceptanceError("actor_file_root_setup_failed")
        runner = actor.runner(
            "/app", CommandPolicy(allowed_commands=frozenset({"python", "python3", "node", "git"}))
        )
        runtime = build_extension_runtime(
            group=group,
            workspace=workspace,
            model=model,
            environment=RuntimeEnvironment(
                runner,
                DockerFileBackend(runner, worker_runtime="node" if language == "js" else "python"),
                host_storage_isolated=True,
            ),
            budget=budget,
            test_argv=task.test_argv,
            memory=memory,
        )
        runtime.events.subscribe(
            "*", JsonlTraceWriter(directory / "trace.jsonl", truncate_on_start=True)
        )
        try:
            async with asyncio.timeout(180):
                result = await runtime.agent.run(task.goal, run_id=run_id)
        except TimeoutError:
            result = await runtime.sessions.load(run_id)
        finally:
            if runtime.close is not None:
                await runtime.close()
        if result is not None:
            write_json(directory / "result.json", asdict(result))
        write_json(directory / "requests.json", [asdict(request) for request in model.requests])
        if actor.alive:
            await actor.kill()
        # Docker cp works for a stopped container too, retaining failure/cancelled
        # candidates. The parent stores raw binary bytes without following links.
        capture_started = time.monotonic()
        receipt = await host.run(
            ("docker", "cp", actor.name + ":/app/.", "-"), timeout_seconds=10, binary_output=archive
        )
        write_json(
            directory / "capture.json",
            {**asdict(receipt), "duration_ms": (time.monotonic() - capture_started) * 1000},
        )
        captured = receipt.exit_code == 0 and not receipt.timed_out
    removed = await host.run(("docker", "inspect", actor.name))
    if removed.exit_code == 0:
        raise AcceptanceError("actor_container_not_removed")
    accepted = None
    verifier_ms = None
    if captured:
        write_json(
            directory / "artifact_hashes.json", await asyncio.to_thread(archive_hashes, archive)
        )
        async with DockerSandbox(host, image=image, resources=resources) as judge:
            await record_world(host, judge, directory / "evaluator_container.json")
            copied = await host.run(
                ("docker", "cp", str(archive), judge.name + ":/tmp/candidate.tar")
            )
            if copied.exit_code:
                raise AcceptanceError("candidate_transfer_failed")
            # Docker Desktop may retain the host uid even without cp --archive.
            # Keep the host capture private; only its isolated, unmounted copy is
            # made readable by its numeric owner. No container capabilities added.
            owner = archive.stat()
            readable = await host.run(
                (
                    "docker",
                    "exec",
                    "--user",
                    f"{owner.st_uid}:{owner.st_gid}",
                    judge.name,
                    "chmod",
                    "0444",
                    "/tmp/candidate.tar",
                )
            )
            write_json(directory / "candidate_transfer_permissions.json", asdict(readable))
            if readable.exit_code:
                raise AcceptanceError("candidate_transfer_permissions_failed")
            made = await judge.execute(("mkdir", "-p", "/app"))
            if made.exit_code:
                raise AcceptanceError("evaluator_workspace_failed")
            cache_flags = (
                ("--exclude=__pycache__", "--exclude=*.pyc") if language == "python" else ()
            )
            extracted = await judge.execute(
                ("tar", "--no-same-owner", *cache_flags, "-xf", "/tmp/candidate.tar", "-C", "/app"),
                timeout_seconds=10,
            )
            write_json(directory / "candidate_extraction.json", asdict(extracted))
            if extracted.exit_code:
                raise AcceptanceError("candidate_extraction_failed")
            copied = await host.run(
                ("docker", "cp", str(task.verification_overlay), judge.name + ":/acceptance")
            )
            if copied.exit_code:
                raise AcceptanceError("criterion_transfer_failed")
            deployment = await judge.execute(
                (
                    interpreter,
                    "-e" if language == "js" else "-c",
                    _GRADE_JS if language == "js" else _GRADE_PY,
                ),
                timeout_seconds=10,
            )
            write_json(directory / "criterion_deployment.json", asdict(deployment))
            if deployment.exit_code:
                raise AcceptanceError("criterion_deployment_failed")
            began = time.monotonic()
            verdict = await judge.execute(task.success_argv, cwd="/app", timeout_seconds=10)
            verifier_ms = (time.monotonic() - began) * 1000
            write_json(directory / "verifier.json", asdict(verdict))
            accepted = verdict.exit_code == 0 and not verdict.timed_out
        removed = await host.run(("docker", "inspect", judge.name))
        if removed.exit_code == 0:
            raise AcceptanceError("evaluator_container_not_removed")
    sidecar = ScoringEvidence(
        run_id=run_id,
        case_id=task.task_id,
        source=f"independent isolated overlay {task.verification_commit}",
        task_success=accepted,
        budget=limits,
    )
    (directory / "evidence.json").write_text(sidecar.model_dump_json(indent=2) + "\n")
    trace = TraceRun.from_file(directory / "trace.jsonl")
    score = score_harness(trace, evidence=sidecar)
    facts = {
        "run_id": run_id,
        "case_id": task.task_id,
        "task_success": accepted,
        "artifact_captured": captured,
        "actor_removed": True,
        "independent_duration_ms": verifier_ms,
        "score": score.to_json(),
        "provider_invocations": model.calls,
        "status": "checked" if accepted is not None else "unverified",
    }
    write_json(directory / "check.json", facts)
    return facts


async def run_isolated_matrix(  # noqa: PLR0912, PLR0915 - fixed once-only sequential cohort lifecycle
    snapshot: Path, output: Path, *, provider: str, phase: Path | None, credentials: Path | None
) -> None:
    snapshot = await asyncio.to_thread(snapshot.resolve)
    output = await asyncio.to_thread(output.resolve)
    frozen = await asyncio.to_thread(check, snapshot)
    if frozen.get("memory_seed") is None:
        raise AcceptanceError("approved_frozen_seeds_required")
    if provider == "deepseek":
        if phase is None:
            raise AcceptanceError("real_queue_requires_pr4_acceptance_and_approved_frozen_seeds")
        await asyncio.to_thread(check_phase, phase, frozen)
    approval = _SeedApproval.model_validate_json(
        await asyncio.to_thread(read_seed_file, snapshot / "memory_approval.json")
    )
    if provider == "deepseek" and (
        approval.authorization_kind != "explicit_user_approval"
        or approval.protocol_sha256 != frozen["protocol_sha256"]
    ):
        raise AcceptanceError("human_seed_approval_required")
    original_profile = _SeedProfile.model_validate_json(
        read_seed_file(snapshot / "memory_seed.json")
    )
    reservation = Path(original_profile.approval.path).parent / "real_queue_reserved.json"
    if provider == "deepseek" and (reservation.exists() or reservation.is_symlink()):
        raise AcceptanceError("real_queue_already_reserved")
    if await asyncio.to_thread(output.exists):
        raise AcceptanceError("new_execution_directory_required")
    protocol = frozen["protocol"]
    host = DockerHost()
    native = await host.run(("docker", "info", "--format", "{{.Architecture}}"))
    if native.exit_code or native.stdout.strip() not in {"aarch64", "arm64"}:
        raise AcceptanceError("native_container_architecture_required")
    for image in protocol["isolation"]["images"]["images"].values():
        inspected = await host.run(("docker", "image", "inspect", image))
        if inspected.exit_code:
            raise AcceptanceError("pinned_image_missing")
        checked_image_size(inspected.stdout, image, "arm64")
    shared = provider_model(protocol, credentials) if provider == "deepseek" else None
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    if provider == "deepseek":
        write_private(
            reservation,
            canonical(
                {
                    "protocol_sha256": frozen["protocol_sha256"],
                    "approval_sha256": original_profile.approval.sha256,
                    "source_sha256": frozen["source_sha256"],
                    "output": str(output),
                    "declared_runs": REAL_RUNS,
                    "total_token_budget": TOKEN_BUDGET,
                }
            ),
        )
        write_private(
            snapshot / "real_queue_reserved.json",
            canonical(
                {
                    "source_sha256": frozen["source_sha256"],
                    "output": str(output),
                    "declared_runs": REAL_RUNS,
                    "total_token_budget": TOKEN_BUDGET,
                }
            ),
        )
    execution = {
        "schema_version": "1.0",
        "mode": "isolated_matrix",
        "provider": provider,
        "source_sha256": frozen["source_sha256"],
        "snapshot_manifest_sha256": digest((snapshot / "manifest.json").read_bytes()),
        "real_model_api_calls": 0,
        "seed_authorization_kind": approval.authorization_kind,
        "provider_call_scope": "adapter invocations, max_attempts=1",
        "declared_rows": REAL_RUNS,
        "attempted_rows": 0,
        "known_tokens": 0,
        "nominal_total_token_budget": TOKEN_BUDGET,
        "status": "running",
        "stop_reason": None,
        "real_queue_scope": "one frozen 90-row cohort"
        if provider == "deepseek"
        else "offline native rehearsal; no real-model results",
    }
    write_json(output / "execution.json", execution)
    tasks = {
        task.task_id: task
        for task in BenchmarkManifest.from_file(ROOT / "benchmarks/extension_v1/offline.json").tasks
    }
    try:
        for index, declared in enumerate(protocol["queue"]):
            if execution["known_tokens"] >= TOKEN_BUDGET:
                execution["stop_reason"] = "total_token_budget"
                break
            await asyncio.to_thread(check, snapshot)
            directory = output / "runs" / queue_id(index, declared)
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=False)
            task = tasks[declared["case_id"]]
            model = ObservedModel(
                shared if shared is not None else ScriptedModel.from_file(task.response_script)
            )
            execution["attempted_rows"] += 1
            write_json(output / "execution.json", execution)
            try:
                facts = await isolated_row(
                    snapshot,
                    directory,
                    declared,
                    task,
                    protocol,
                    host,
                    model,
                    TOKEN_BUDGET - execution["known_tokens"],
                )
            finally:
                if provider == "deepseek":
                    execution["real_model_api_calls"] += model.calls
                trace_path = directory / "trace.jsonl"
                if trace_path.exists():
                    try:
                        usage = score_harness(TraceRun.from_file(trace_path)).measurements
                        execution["known_tokens"] += usage["total_tokens"]
                    except (TraceError, ValueError):
                        execution["stop_reason"] = "unreadable_usage_trace"
            measurements = facts["score"]["measurements"]
            write_json(output / "execution.json", execution)
            if measurements["token_usage_complete"] is not True:
                execution["stop_reason"] = "unknown_or_unfinished_usage"
                break
            if facts["status"] == "unverified":
                execution["stop_reason"] = "candidate_unavailable"
                break
        execution["status"] = "stopped" if execution["stop_reason"] is not None else "completed"
    except BaseException:
        execution["status"] = "interrupted"
        raise
    finally:
        write_json(output / "execution.json", execution)
    if execution["status"] == "stopped":
        raise AcceptanceError("matrix_stopped_" + str(execution["stop_reason"]))


def comparison_run(
    declared: dict[str, Any], score: HarnessMetrics | None, trace: TraceRun | None
) -> ComparisonRun:
    """Extract quantities from facts; never infer a memory opportunity from task text."""
    identity = (declared["case_id"], declared["repeat"], declared["group"])
    if score is None or trace is None:
        return ComparisonRun(*identity, observed=False)
    success = score.rates["task_success_rate"]
    values = score.measurements
    tokens = values.get("total_tokens")
    duration = values.get("duration_ms")
    matches = []
    exposures = []
    if declared["group"] == "D":
        for record in trace.records:
            metadata = record.data.get("metadata")
            if record.name == "context_built" and isinstance(metadata, dict):
                count = metadata.get("memory_match_count")
                if type(count) is int and count >= 0:
                    matches.append(count > 0)
            if record.name == "model_call_started":
                messages = record.data.get("messages")
                if isinstance(messages, list):
                    exposures.append(
                        any(
                            isinstance(message, dict)
                            and message.get("role") == "user"
                            and message.get("name") == "project_memory"
                            for message in messages
                        )
                    )
    return ComparisonRun(
        *identity,
        task_success=success.numerator == 1 if success.denominator == 1 else None,
        tokens=tokens if type(tokens) is int else None,
        usage_complete=values.get("token_usage_complete") is True,
        finalized=values.get("finalized") is True,
        latency_ms=float(duration) if type(duration) in {int, float} else None,
        memory_match=any(matches) if matches else None,
        memory_exposed=any(exposures) if exposures else None,
    )


def metric_label(metric: MetricComparison) -> str:
    """Display precision does not change the full numeric JSON evidence."""
    if metric.delta is None:
        return "unmeasured"
    interval = (
        f"[{metric.interval_95[0]:.4g}, {metric.interval_95[1]:.4g}]"
        if metric.interval_95 is not None
        else "interval unavailable"
    )
    return f"{metric.delta:.4g}; {interval}"


def report(  # noqa: PLR0915 - preserve the declared cohort while deriving all report views
    snapshot: Path, output: Path, evidence: Path | None, *, phase: Path | None = None
) -> None:
    """Derive from raw Trace and independent sidecars, retaining every opportunity."""
    manifest = check(snapshot, current=False)
    phase_receipt = None
    if phase is not None:
        phase_bytes = read_seed_file(phase.resolve())
        check_phase(phase, manifest)
        read_seed_file(phase.resolve(), digest(phase_bytes))
        phase_receipt = {"sha256": digest(phase_bytes), "source_sha256": manifest["source_sha256"]}
    execution = None
    if evidence is not None:
        execution = json.loads((evidence / "execution.json").read_text())
        if execution["source_sha256"] != manifest["source_sha256"] or execution[
            "snapshot_manifest_sha256"
        ] != digest((snapshot / "manifest.json").read_bytes()):
            raise AcceptanceError("evidence_source_mismatch")
    scores = []
    rows = []
    paired_rows = []
    grouped_scores = {group.value: [] for group in ExtensionGroup}
    for index, declared in enumerate(manifest["protocol"]["queue"]):
        identity = queue_id(index, declared)
        row = {**declared, "id": identity, "run_id": None, "status": "not_run"}
        directory = evidence / "runs" / identity if evidence is not None else None
        paired = comparison_run(declared, None, None)
        if directory is not None and (directory / "trace.jsonl").is_file():
            row["inputs_sha256"] = {
                name: digest((directory / name).read_bytes())
                for name in ("trace.jsonl", "evidence.json", "check.json", "configuration.json")
                if (directory / name).is_file()
            }
            try:
                trace = TraceRun.from_file(directory / "trace.jsonl")
                if not trace.records:
                    raise TraceError("empty trace")
                sidecar = directory / "evidence.json"
                checks = ScoringEvidence.from_file(sidecar) if sidecar.exists() else None
                if checks is not None and checks.case_id != declared["case_id"]:
                    raise AcceptanceError("evidence_case_mismatch")
                if checks is None:
                    configuration = directory / "configuration.json"
                    limits = (
                        json.loads(configuration.read_text())["budget"]
                        if configuration.exists()
                        else manifest["protocol"]["budget"]
                    )
                    checks = ScoringEvidence(
                        run_id=trace.records[0].run_id,
                        case_id=declared["case_id"],
                        source="frozen_budget_without_task_verdict",
                        budget=limits,
                    )
                score = score_harness(trace, evidence=checks)
                scores.append(score)
                grouped_scores[declared["group"]].append(score)
                paired = comparison_run(declared, score, trace)
                row.update(run_id=score.run_id, status="observed", score=score.to_json())
            except TraceError:
                row["status"] = "unreadable_trace"
        rows.append(row)
        paired_rows.append(paired)
    aggregate = aggregate_harness_metrics(scores)
    success = [s.rates["task_success_rate"] for s in scores]
    successes = sum(s.numerator for s in success)
    known_tokens = sum(int(str(s.measurements["total_tokens"])) for s in scores)
    complete = len(scores) == len(rows) and all(
        s.measurements["token_usage_complete"] is True for s in scores
    )
    paired_report = compare_extension_groups(
        paired_rows,
        case_ids=tuple(dict.fromkeys(row["case_id"] for row in manifest["protocol"]["queue"])),
        safety_passed=True if phase_receipt is not None else None,
        real_model_results=bool(
            execution
            and execution.get("provider") == "deepseek"
            and execution.get("status") == "completed"
            and execution.get("real_model_api_calls", 0) > 0
        ),
    )
    payload = {
        "schema_version": "1.0",
        "derived": True,
        "execution": execution,
        "execution_sha256": digest((evidence / "execution.json").read_bytes())
        if evidence is not None
        else None,
        "real_model_api_calls": execution["real_model_api_calls"] if execution else 0,
        "source_sha256": manifest["source_sha256"],
        "input_manifest_sha256": digest((snapshot / "manifest.json").read_bytes()),
        "scorer_sha256": {
            name: digest((ROOT / name).read_bytes())
            for name in (
                "scripts/extension_acceptance.py",
                "src/minidsh/evaluation/harness_metrics.py",
                "src/minidsh/evaluation/extension_comparison.py",
                "src/minidsh/evaluation/tracefile.py",
            )
        },
        "declared_runs": len(rows),
        "executed_runs": len(scores),
        "task_success_rate": MetricRate(
            successes, sum(s.denominator for s in success), len(rows)
        ).to_json(),
        "known_tokens": known_tokens,
        "total_tokens": known_tokens if complete else None,
        "token_usage_complete": complete,
        "tokens_per_success": known_tokens / successes if complete and successes else None,
        "known_tokens_per_success": known_tokens / successes if successes else None,
        "known_unknown_usage_calls": sum(
            int(str(s.measurements["unknown_usage_calls"])) for s in scores
        ),
        "unobserved_rows": len(rows) - len(scores),
        "metrics": aggregate,
        "group_metrics": {
            name: aggregate_harness_metrics(items) for name, items in grouped_scores.items()
        },
        "group_metrics_scope": "descriptive observed-trace counters only; full declared-cohort "
        "quantities and coverage are in paired_comparison.groups",
        "paired_comparison": paired_report.to_json(),
        "accepted_safety_receipt": phase_receipt,
        "results": rows,
        "conclusion": "Exploratory paired estimates; no phase acceptance is inferred. "
        "Benefit labels require a complete real cohort and accepted safety receipt.",
    }
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "report.json", payload)
    write_json(
        output / "comparisons.json",
        {
            "actor_source_sha256": manifest["source_sha256"],
            "scorer_sha256": payload["scorer_sha256"],
            "accepted_safety_receipt": phase_receipt,
            "comparison": paired_report.to_json(),
            "derived": True,
        },
    )
    write_json(
        output / "coverage.json",
        [
            {
                "case_id": case["case_id"],
                "status": "untested",
                "reason": "report does not perform phase acceptance; source receipt is bound"
                if phase_receipt is not None
                else "no phase receipt supplied",
            }
            for case in manifest["protocol"]["cases"]
        ],
    )
    (output / "report.md").write_text(
        "# Extension v1 derived report\n\n"
        f"Observed {len(scores)}/{len(rows)} declared rows; independently accepted {successes}.\n\n"
        "Missing evidence remains unknown. finished does not imply task success.\n"
        "Paired estimates are exploratory. Safety and real-cohort gates control benefit labels.\n\n"
        "| Contrast | Delta success fraction | Delta Token/success | Delta Loop mean ms | "
        "Assessment |\n"
        "| --- | --- | --- | --- | --- |\n"
        + "".join(
            f"| {name} | {metric_label(item.metrics['success_rate'])} | "
            f"{metric_label(item.metrics['tokens_per_success'])} | "
            f"{metric_label(item.metrics['latency_mean_ms'])} | {item.benefit} |\n"
            for name, item in paired_report.comparisons.items()
        )
        + "\nValues include 95% intervals. Undefined replicate counts are in "
        "[comparisons.json](comparisons.json); "
        "None means unmeasured. Latency uses the recorded Loop duration; "
        "composition, finishing/end cleanup, capture and independent verification are excluded.\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freezing = sub.add_parser("freeze")
    freezing.add_argument("--output", type=Path, required=True)
    freezing.add_argument("--seed-manifest", type=Path)
    checking = sub.add_parser("check")
    checking.add_argument("--snapshot", type=Path, required=True)
    checking.add_argument("--archive-only", action="store_true")
    checking.add_argument("--seed-manifest", type=Path)
    checking.add_argument("--pr4-acceptance", type=Path)
    running = sub.add_parser("run")
    running.add_argument("--snapshot", type=Path, required=True)
    running.add_argument("--output", type=Path, required=True)
    running.add_argument("--provider", choices=("scripted", "deepseek"), default="scripted")
    running.add_argument("--isolated-matrix", action="store_true")
    running.add_argument("--pr4-acceptance", type=Path)
    running.add_argument("--credentials-file", type=Path)
    reporting = sub.add_parser("report")
    reporting.add_argument("--snapshot", type=Path, required=True)
    reporting.add_argument("--output", type=Path, required=True)
    reporting.add_argument("--evidence", type=Path)
    reporting.add_argument("--pr4-acceptance", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "freeze":
            freeze(args.output, args.seed_manifest)
        elif args.command == "check":
            frozen = check(args.snapshot, current=not args.archive_only)
            if args.seed_manifest is not None:
                asyncio.run(load_seed(args.seed_manifest, workspace=args.snapshot))
            if args.pr4_acceptance is not None:
                check_phase(args.pr4_acceptance, frozen)
        elif args.command == "report":
            report(args.snapshot, args.output, args.evidence, phase=args.pr4_acceptance)
        elif args.command == "run":
            if args.provider == "deepseek" or args.isolated_matrix:
                asyncio.run(
                    run_isolated_matrix(
                        args.snapshot,
                        args.output,
                        provider=args.provider,
                        phase=args.pr4_acceptance,
                        credentials=args.credentials_file,
                    )
                )
            else:
                asyncio.run(run_offline(args.snapshot, args.output))
        sys.stdout.write("passed\n")
        return 0
    except (AcceptanceError, OSError, ValueError, KeyError, tarfile.TarError) as exc:
        code = str(exc) if isinstance(exc, AcceptanceError) else "invalid_acceptance_input"
        sys.stderr.write(code + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
