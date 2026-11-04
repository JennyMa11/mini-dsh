"""Native, resource-bounded HumanEvalFix-docs subset with independent grading."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import re
import secrets
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field, ValidationError

from minidsh.adapters import JsonlTraceWriter
from minidsh.benchmark.public_swe import (
    PublicStudyError,
    UsageModel,
    download,
    git_object_hash,
    make_provider_model,
    run_checked,
    summarize,
    write_json,
)
from minidsh.composition import RuntimeComponents, RuntimeOptions, build_local_runtime
from minidsh.domain import AgentBudget, ModelCallError
from minidsh.infrastructure.containers import ContainerResources, DockerHost, DockerSandbox
from minidsh.infrastructure.process import CommandPolicy, ProcessResult, ProcessRunner
from minidsh.ports import Model
from minidsh.tools import ShellTool

Language = Literal["python", "js"]
_UPSTREAM = "https://github.com/bigcode-project/octopack"
_TAGS = {
    "python": "docker.io/library/python:3.12-slim-bookworm",
    "js": "docker.io/library/node:22-bookworm-slim",
}
COMMANDS = frozenset(
    {"python", "python3", "node", "ls", "cat", "find", "grep", "sed", "head", "tail", "wc"}
)
_MAX_CANDIDATE = 65_536
_TASK_COUNT = 12
_LANGUAGE_TASK_COUNT = 6


class PublicLiteError(PublicStudyError):
    """A source, resource or verifier gate prevented the lightweight study."""


class _SourceFile(BaseModel):
    filename: Literal["python.jsonl", "js.jsonl"]
    upstream_path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    git_blob_sha1: str = Field(pattern=r"^[a-f0-9]{40}$")


class _Source(BaseModel):
    repository: Literal["https://github.com/bigcode-project/octopack"]
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    files: dict[Language, _SourceFile]


class _Protocol(BaseModel):
    model: Literal["deepseek-flash"]
    temperature: Literal[0]
    thinking: Literal[False]
    repeats: Literal[1]
    agent_count: Literal[1]
    total_run_token_budget: Literal[240000]
    max_tokens_per_task: Literal[20000]
    max_steps: Literal[16]
    max_tool_calls: Literal[32]
    max_duration_seconds: Literal[180]
    verifier_timeout_seconds: Literal[10]
    memory_mb: Literal[1024]
    cpus: Literal[1]
    pids_limit: Literal[128]
    cost_metrics: Literal[False]


class LiteTask(BaseModel):
    """Identity and provenance of one frozen public repair task."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    language: Language
    upstream_id: str = Field(pattern=r"^(Python|JavaScript)/[0-9]+$")
    entry_point: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    bug_type: str
    coverage_tags: list[str]
    record_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    initial_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Record(BaseModel):
    task_id: str
    prompt: str
    buggy_solution: str
    canonical_solution: str
    example_test: str
    test: str
    docstring: str
    entry_point: str
    bug_type: str


class LiteSelection(BaseModel):
    """Versioned protocol that forbids heavy images and concurrent agents."""

    schema_version: Literal["1.0"]
    kind: Literal["public_lightweight_selection"]
    name: str
    source: _Source
    run_protocol: _Protocol
    tasks: list[LiteTask] = Field(min_length=12, max_length=12)

    @classmethod
    def from_file(cls, path: Path) -> LiteSelection:
        """Validate source paths, task identities and the small resource protocol."""
        try:
            selection = cls.model_validate_json(path.read_text())
        except (OSError, ValidationError) as exc:
            raise PublicLiteError("invalid lightweight selection") from exc
        if set(selection.source.files) != {"python", "js"}:
            raise PublicLiteError("both language sources are required")
        for language, source in selection.source.files.items():
            if source.filename != language + ".jsonl" or source.upstream_path != (
                f"evaluation/create/humaneval-x/data/{language}/data/humanevalpack.jsonl"
            ):
                raise PublicLiteError("invalid public source path")
        if (
            len({t.id for t in selection.tasks}) != _TASK_COUNT
            or len({t.upstream_id for t in selection.tasks}) != _TASK_COUNT
        ):
            raise PublicLiteError("duplicate tasks")
        if any(
            sum(t.language == language for t in selection.tasks) != _LANGUAGE_TASK_COUNT
            for language in ["python", "js"]
        ):
            raise PublicLiteError("expected six tasks per language")
        for task in selection.tasks:
            prefix = "Python/" if task.language == "python" else "JavaScript/"
            if not task.upstream_id.startswith(prefix):
                raise PublicLiteError("task language mismatch")
        return selection

    def records(self, root: Path) -> dict[str, Record]:
        """Validate complete source blobs and selected records before execution."""
        records: dict[str, Record] = {}
        for language, source in self.source.files.items():
            path = root / source.filename
            if path.is_symlink():
                raise PublicLiteError("source symlinks are not supported")
            body = path.read_bytes()
            _verify_source(body, source)
            raw = [json.loads(line) for line in body.splitlines()]
            by_id = {row["task_id"]: row for row in raw}
            if len(by_id) != len(raw):
                raise PublicLiteError("duplicate upstream records")
            for task in self.tasks:
                if task.language != language:
                    continue
                value = by_id.get(task.upstream_id)
                if value is None or _record_digest(value) != task.record_sha256:
                    raise PublicLiteError("record mismatch: " + task.id)
                record = Record.model_validate(value)
                initial = record.prompt + record.buggy_solution
                if hashlib.sha256(initial.encode()).hexdigest() != task.initial_file_sha256:
                    raise PublicLiteError("initial source mismatch: " + task.id)
                if record.entry_point != task.entry_point or record.bug_type != task.bug_type:
                    raise PublicLiteError("task metadata mismatch: " + task.id)
                records[task.id] = record
        return records


def _record_digest(record: object) -> str:
    body = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _verify_source(body: bytes, source: _SourceFile) -> None:
    if len(body) > 2 * 1024 * 1024:
        raise PublicLiteError("source file exceeds lightweight size limit")
    if (
        hashlib.sha256(body).hexdigest() != source.sha256
        or git_object_hash("blob", body).hex() != source.git_blob_sha1
    ):
        raise PublicLiteError("source digest mismatch")


def prepare_sources(selection: LiteSelection, root: Path) -> None:
    """Prefer a public source proxy, preserving upstream blob and record hashes."""
    receipts: list[dict[str, str]] = []
    for source in selection.source.files.values():
        path = root / source.filename
        if path.exists():
            _verify_source(path.read_bytes(), source)
            receipts.append({"filename": source.filename, "transport": "verified_local_cache"})
            continue
        official = f"https://raw.githubusercontent.com/bigcode-project/octopack/{selection.source.commit}/{source.upstream_path}"
        for url in ["https://gh-proxy.com/" + official, official]:
            try:
                body = download(url)
                _verify_source(body, source)
            except (OSError, PublicStudyError):
                if url == official:
                    raise
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(body)
            temporary.replace(path)
            receipts.append({"filename": source.filename, "transport": url})
            break
    selection.records(root)
    write_json(root / "downloads.json", receipts)


def source_filename(language: Language) -> str:
    return "solution.py" if language == "python" else "solution.js"


def _verifier(language: Language, tests: str, marker: str) -> str:
    if language == "python":
        return (
            "from pathlib import Path\n"
            "scope = {'__name__': '__main__'}\n"
            "exec(compile(Path('solution.py').read_text(), 'solution.py', 'exec'), scope)\n"
            f"exec(compile({tests!r}, '<tests>', 'exec'), scope)\n"
            f"print({marker!r})\n"
        )
    return (
        "const fs = require('node:fs');\n"
        "const vm = require('node:vm');\n"
        "const assert = require('node:assert');\n"
        "const context = {};\n"
        "Object.defineProperty(context, 'console', {value: Object.freeze({"
        "assert: (condition) => assert.ok(condition), log: () => {}}), writable: false});\n"
        "vm.createContext(context);\n"
        "vm.runInContext(fs.readFileSync('solution.js', 'utf8'), context, {timeout: 5000});\n"
        f"vm.runInContext({json.dumps(tests)}, context, {{timeout: 5000}});\n"
        f"process.stdout.write({json.dumps(marker + chr(10))});\n"
    )


def fixture_files(language: Language, record: Mapping[str, str]) -> dict[str, str]:
    """Expose the defective implementation, contract and public examples only."""
    extension = "py" if language == "python" else "js"
    return {
        source_filename(language): record["prompt"] + record["buggy_solution"],
        "smoke." + extension: _verifier(language, record["example_test"], "examples_passed"),
        "README.md": "Fix the existing implementation according to this contract:\n\n"
        + record["docstring"]
        + "\n",
    }


def evaluate_output(result: ProcessResult, marker: str) -> bool:
    """Require completed tests; Node assertion diagnostics are never success."""
    return (
        result.exit_code == 0
        and not result.timed_out
        and result.stdout.splitlines()[-1:] == [marker]
        and "Assertion failed" not in result.stderr
    )


def _progress(payload: object) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def container_limits(platform: str) -> ContainerResources:
    return ContainerResources(platform=platform, memory_mb=1024, cpus=1, pids_limit=128)


async def seed_container(host: ProcessRunner, box: DockerSandbox, directory: Path) -> None:
    made = await box.execute(("mkdir", "-p", "/app"))
    if made.exit_code:
        raise PublicLiteError("cannot prepare container workspace")
    await run_checked(host, ("docker", "cp", str(directory) + "/.", box.name + ":/app"))


def checked_image_size(raw: str, reference: str, architecture: str) -> int:
    image = json.loads(raw)[0]
    expected = {reference, reference.removeprefix("docker.io/library/")}
    if (
        image["Architecture"] != architecture
        or image["Os"] != "linux"
        or not expected.intersection(image.get("RepoDigests", []))
    ):
        raise PublicLiteError("native image digest or architecture mismatch")
    size = int(image["Size"])
    if size > 500 * 1024 * 1024:
        raise PublicLiteError("image exceeds lightweight size limit")
    return size


async def native_platform(host: ProcessRunner) -> str:
    machine = (await run_checked(host, ("docker", "info", "--format", "{{.Architecture}}"))).strip()
    architecture = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(
        machine
    )
    if architecture is None:
        raise PublicLiteError("unsupported native Docker architecture")
    return "linux/" + architecture


async def resolve_images(host: ProcessRunner, output: Path) -> dict[str, object]:
    """Resolve only small native images, preferring the domestic Docker Hub proxy."""
    platform = await native_platform(host)
    architecture = platform.split("/")[1]
    images: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for language, tag in _TAGS.items():
        raw = json.loads(
            await run_checked(host, ("docker", "manifest", "inspect", "--verbose", tag))
        )
        manifests = raw if isinstance(raw, list) else [raw]
        native = [
            m
            for m in manifests
            if m.get("Descriptor", {}).get("platform", {}).get("architecture") == architecture
            and m["Descriptor"]["platform"].get("os") == "linux"
        ]
        if len(native) != 1:
            raise PublicLiteError("expected exactly one native platform manifest")
        descriptor = native[0]["Descriptor"]
        digest = descriptor.get("digest")
        if not isinstance(digest, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", digest) is None:
            raise PublicLiteError("invalid official image digest")
        manifest = native[0].get("SchemaV2Manifest", native[0].get("OCIManifest", {}))
        if sum(layer["size"] for layer in manifest.get("layers", [])) > 200 * 1024 * 1024:
            raise PublicLiteError("compressed image exceeds lightweight size limit")
        root = tag.split(":", 1)[0]
        canonical = root + "@" + digest
        cached = await host.run(("docker", "image", "inspect", canonical))
        if not cached.exit_code and not cached.timed_out:
            reference, size = canonical, checked_image_size(cached.stdout, canonical, architecture)
        else:
            for reference in [canonical.replace("docker.io/", "docker.1ms.run/"), canonical]:
                try:
                    await run_checked(
                        host,
                        ("docker", "pull", "--platform", "linux/" + architecture, reference),
                        900,
                    )
                    info = await run_checked(host, ("docker", "image", "inspect", reference))
                    size = checked_image_size(info, reference, architecture)
                    break
                except PublicStudyError:
                    if reference == canonical:
                        raise
        images[language], sizes[language] = reference, size
        _progress({"language": language, "image": reference, "size_bytes": size})
    payload: dict[str, object] = {
        "platform": platform,
        "images": images,
        "size_bytes": sizes,
    }
    write_json(output / "images.json", payload)
    return payload


def _fingerprint(selection: LiteSelection, images: dict[str, object]) -> str:
    source = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(Path("src").rglob("*.py"))
    }
    source["uv.lock"] = hashlib.sha256(Path("uv.lock").read_bytes()).hexdigest()
    payload = (
        selection.model_dump_json()
        + json.dumps(images, sort_keys=True)
        + json.dumps(source, sort_keys=True)
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def verify_candidate(  # noqa: PLR0913 - explicit independent grading boundary
    host: ProcessRunner,
    *,
    task: LiteTask,
    record: Record,
    candidate: str,
    image: str,
    platform: str,
    logs: Path,
) -> bool:
    """Grade one source file in a fresh container with sealed original tests."""
    await asyncio.to_thread(logs.mkdir, parents=True, exist_ok=True)
    seed = logs / "input"
    seed.mkdir(exist_ok=True)
    (seed / source_filename(task.language)).write_text(candidate)
    marker = "verified_" + secrets.token_hex(16)
    script = "verify.py" if task.language == "python" else "verify.js"
    (seed / script).write_text(_verifier(task.language, record.test, marker))
    interpreter = "python3" if task.language == "python" else "node"
    async with DockerSandbox(host, image=image, resources=container_limits(platform)) as box:
        await seed_container(host, box, seed)
        result = await box.execute((interpreter, script), cwd="/app", timeout_seconds=10)
        passed = evaluate_output(result, marker)
    (logs / "stdout.txt").write_text(result.stdout)
    (logs / "stderr.txt").write_text(result.stderr)
    write_json(
        logs / "result.json",
        {"passed": passed, "exit_code": result.exit_code, "timed_out": result.timed_out},
    )
    return passed


async def check_environment(
    selection: LiteSelection, sources: Path, output: Path, host: ProcessRunner
) -> None:
    """Check all reference/buggy pairs before permitting any model calls."""
    records = selection.records(sources)
    images = await resolve_images(host, output)
    platform = str(images["platform"])
    references = cast("dict[str, str]", images["images"])
    gates: list[dict[str, object]] = []
    for task in selection.tasks:
        record = records[task.id]
        gold = await verify_candidate(
            host,
            task=task,
            record=record,
            candidate=record.prompt + record.canonical_solution,
            image=references[task.language],
            platform=platform,
            logs=output / "gates" / task.id / "reference",
        )
        buggy = await verify_candidate(
            host,
            task=task,
            record=record,
            candidate=record.prompt + record.buggy_solution,
            image=references[task.language],
            platform=platform,
            logs=output / "gates" / task.id / "buggy",
        )
        row: dict[str, object] = {
            "task_id": task.id,
            "reference_passed": gold,
            "buggy_passed": buggy,
        }
        gates.append(row)
        write_json(
            output / "gates.json", {"fingerprint": _fingerprint(selection, images), "tasks": gates}
        )
        _progress(row)
        if not gold or buggy:
            raise PublicLiteError("reference/buggy gate failed: " + task.id)


async def capture_candidate(box: DockerSandbox, language: Language) -> str | None:
    filename = source_filename(language)
    if language == "python":
        argv = (
            "python3",
            "-c",
            "import base64,sys; from pathlib import Path; "
            "data=Path(sys.argv[1]).read_bytes(); assert len(data)<=65536; "
            "print(base64.b64encode(data).decode())",
            filename,
        )
    else:
        argv = (
            "node",
            "-e",
            "const b=require('node:fs').readFileSync(process.argv[1]); "
            "if(b.length>65536)process.exit(1); console.log(b.toString('base64'));",
            filename,
        )
    result = await box.execute(argv, cwd="/app", timeout_seconds=10)
    if result.exit_code or result.timed_out:
        return None
    try:
        body = base64.b64decode(result.stdout.strip(), validate=True)
        return body.decode() if len(body) <= _MAX_CANDIDATE else None
    except (ValueError, UnicodeError):
        return None


def prepare_fixture(directory: Path, task: LiteTask, record: Record) -> Path:
    seed = directory / "agent_input"
    seed.mkdir(parents=True, exist_ok=True)
    for name, content in fixture_files(
        task.language, cast("dict[str, str]", record.model_dump())
    ).items():
        (seed / name).write_text(content)
    return seed


def task_goal(task: LiteTask) -> str:
    interpreter = "python3" if task.language == "python" else "node"
    script = "smoke.py" if task.language == "python" else "smoke.js"
    return (
        f"Fix {source_filename(task.language)} according to README.md. "
        f"Use {interpreter} to inspect and atomically update the file. "
        f"Run `{interpreter} {script}` to check public examples. Deliver the repaired source file."
    )


def _load_gates(selection: LiteSelection, output: Path) -> tuple[dict[str, object], str]:
    try:
        images = json.loads((output / "images.json").read_text())
        gates = json.loads((output / "gates.json").read_text())
    except (OSError, ValueError) as exc:
        raise PublicLiteError("run check before using the model") from exc
    if gates["fingerprint"] != _fingerprint(selection, images) or {
        row["task_id"]
        for row in gates["tasks"]
        if row["reference_passed"] and not row["buggy_passed"]
    } != {t.id for t in selection.tasks}:
        raise PublicLiteError("complete matching reference/buggy gates are required")
    if (output / "report.json").exists():
        raise PublicLiteError("existing results cannot be overwritten; choose a fresh directory")
    return images, str(gates["fingerprint"])


async def run_study(
    selection: LiteSelection,
    *,
    sources: Path,
    output: Path,
    host: ProcessRunner,
    model_factory: Callable[[], Model],
) -> dict[str, object]:
    """Run twelve sequential repairs after matching complete infrastructure gates."""
    records = selection.records(sources)
    await run_checked(host, ("docker", "version", "--format", "{{.Server.Version}}"))
    images, fingerprint = _load_gates(selection, output)
    references = cast("dict[str, str]", images["images"])
    platform = str(images["platform"])
    if platform != await native_platform(host):
        raise PublicLiteError("native platform changed; rerun environment gates")
    rows: list[dict[str, object]] = [
        {
            "task_id": t.id,
            "language": t.language,
            "success": False,
            "tokens": 0,
            "status": "not_run",
        }
        for t in selection.tasks
    ]
    protocol = selection.run_protocol
    consumed, stopped = 0, False
    for task, row in zip(selection.tasks, rows, strict=True):
        if stopped or consumed >= protocol.total_run_token_budget:
            row["failure_type"] = "sweep_stopped" if stopped else "aggregate_budget_exhausted"
        else:
            record = records[task.id]
            directory = output / "runs" / task.id
            seed = prepare_fixture(directory, task, record)
            model = UsageModel(model_factory())
            budget = AgentBudget(
                max_steps=protocol.max_steps,
                max_tokens=min(
                    protocol.max_tokens_per_task, protocol.total_run_token_budget - consumed
                ),
                max_tool_calls=protocol.max_tool_calls,
                max_duration_seconds=protocol.max_duration_seconds,
            )
            candidate = None
            try:
                async with DockerSandbox(
                    host, image=references[task.language], resources=container_limits(platform)
                ) as box:
                    await seed_container(host, box, seed)
                    runner = box.runner("/app", CommandPolicy(COMMANDS))
                    runtime = build_local_runtime(
                        workspace=directory,
                        model=model,
                        budget=budget,
                        options=RuntimeOptions(
                            builtin_tools_enabled=False,
                            allow_shell=True,
                            normalize_tool_results=True,
                            error_recovery_enabled=False,
                            artifact_offload=False,
                            tool_max_retries=0,
                            system_prompt=(
                                "You are a coding agent repairing an existing function. "
                                "Use shell argv arrays to inspect files, atomically patch "
                                "the source with python3 or node, and run public smoke tests. "
                                "Preserve the function interface. Finish with a brief answer."
                            ),
                        ),
                        components=RuntimeComponents(runner=runner, tools=(ShellTool(runner),)),
                    )
                    runtime.events.subscribe(
                        "*", JsonlTraceWriter(directory / "trace.jsonl", truncate_on_start=True)
                    )
                    result = await runtime.agent.run(task_goal(task))
                    consumed += result.token_usage.total_tokens
                    row.update(
                        {
                            "status": result.status.value,
                            "tokens": result.token_usage.total_tokens,
                            "input_tokens": result.token_usage.input_tokens,
                            "output_tokens": result.token_usage.output_tokens,
                            "cached_input_tokens": result.token_usage.cached_input_tokens,
                            "steps": result.steps,
                            "tool_calls": result.tool_calls,
                            "duration_ms": result.duration_ms,
                            "failure_reason": result.failure_reason,
                            "unknown_usage_calls": model.unknown_calls,
                            "token_overrun": max(
                                0, result.token_usage.total_tokens - budget.max_tokens
                            ),
                        }
                    )
                    stopped |= model.unknown_calls > 0
                    if box.alive:
                        candidate = await capture_candidate(box, task.language)
                    if candidate is None:
                        row["failure_type"] = (
                            "candidate_capture_failed" if box.alive else "tool_timeout"
                        )
                if candidate is not None:
                    (directory / source_filename(task.language)).write_text(candidate)
                    row["success"] = await verify_candidate(
                        host,
                        task=task,
                        record=record,
                        candidate=candidate,
                        image=references[task.language],
                        platform=platform,
                        logs=directory / "verifier",
                    )
                    if not row["success"]:
                        row["failure_type"] = "verification_failed"
            except (OSError, PublicStudyError) as exc:
                row.update({"failure_type": "infrastructure_error", "failure_reason": str(exc)})
                stopped = True
        report: dict[str, object] = {
            "schema_version": "1.0",
            "name": selection.name,
            "source_commit": selection.source.commit,
            "fingerprint": fingerprint,
            "model": protocol.model,
            "platform": platform,
            "tools": ["shell"],
            "memory_mb": protocol.memory_mb,
            "cpus": protocol.cpus,
            "agent_network": "none",
            "verifier_network": "none",
            "score_scope": "Custom HumanEvalFix-docs subset; function repair",
            "aggregate": summarize(rows, protocol.total_run_token_budget),
            "results": rows,
        }
        write_json(output / "report.json", report)
        _progress(
            {
                "task": task.id,
                "success": row["success"],
                "tokens": row["tokens"],
                "status": row["status"],
            }
        )
    return report


def snapshot(selection_path: Path, output: Path) -> None:
    """Preserve reproducible harness inputs without copying credentials or caches."""
    files = [
        *sorted(Path("src").rglob("*.py")),
        Path("pyproject.toml"),
        Path("uv.lock"),
        selection_path,
    ]
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    for path in files:
        destination = output / "reproduction" / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    write_json(
        output / "metadata.json",
        {
            "schema_version": "1.0",
            "source_file_sha256": hashes,
            "credentials": "host_memory_only",
            "model_retries": 0,
            "reference_gates_required": True,
        },
    )


def main() -> None:
    """Separate source preparation, native environment gates and model execution."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "check", "run"))
    parser.add_argument(
        "--selection", type=Path, default=Path("benchmarks/public_lite/v1/selection.json")
    )
    parser.add_argument(
        "--sources", type=Path, default=Path("benchmark-results/public-lite/sources")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("benchmark-results/public-lite/baseline")
    )
    parser.add_argument("--credentials-file", type=Path)
    args = parser.parse_args()
    try:
        selection = LiteSelection.from_file(args.selection)
        if args.action == "prepare":
            prepare_sources(selection, args.sources)
        elif args.action == "check":
            if (args.output / "report.json").exists():
                raise PublicLiteError("existing results cannot be overwritten")
            snapshot(args.selection, args.output)
            asyncio.run(check_environment(selection, args.sources, args.output, DockerHost()))
        else:
            asyncio.run(
                run_study(
                    selection,
                    sources=args.sources,
                    output=args.output,
                    host=DockerHost(),
                    model_factory=lambda: make_provider_model(args.credentials_file)(),  # noqa: PLW0108 - defer credential loading
                )
            )
    except (PublicStudyError, OSError, ValueError, ModelCallError) as exc:
        parser.exit(1, "Lightweight public benchmark stopped: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
