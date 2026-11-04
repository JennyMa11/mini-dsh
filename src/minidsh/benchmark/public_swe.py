"""Opt-in SWE-bench Pro bridge; its dataset never enters the core agent loop.

This module also owns the small download, checked-process and JSON helpers shared
by the other public-study runners.

Run with ``uv run python -m minidsh.benchmark.public_swe --help``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import statistics
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field, ValidationError

from minidsh.adapters import JsonlTraceWriter
from minidsh.adapters.models import CredentialRedactingModel
from minidsh.adapters.profiles import build_provider_model, resolve_profile
from minidsh.composition import RuntimeComponents, RuntimeOptions, build_local_runtime
from minidsh.domain import AgentBudget, ModelCallError, ModelRequest, ModelResponse
from minidsh.infrastructure.containers import DockerHost, DockerSandbox
from minidsh.infrastructure.process import CommandPolicy, ProcessRunner
from minidsh.ports import Model
from minidsh.tools import GitTool, ShellTool

_REPOSITORY = "https://github.com/scaleapi/SWE-bench_Pro-os"
_RAW_ROOT = "https://raw.githubusercontent.com/scaleapi/SWE-bench_Pro-os/"
_IMAGE_ROOT = "ghcr.io/scaleapi/swe-bench_pro-v2:"
_MAX_DOWNLOAD = 50 * 1024 * 1024
_COMMANDS = frozenset(
    {
        "python",
        "python3",
        "pytest",
        "node",
        "npm",
        "yarn",
        "pnpm",
        "go",
        "git",
        "rg",
        "grep",
        "find",
        "ls",
        "cat",
        "sed",
        "head",
        "tail",
        "wc",
    }
)


class PublicStudyError(RuntimeError):
    """A source, infrastructure or protocol gate prevented a public study."""


class _Budget(BaseModel):
    max_tokens: int = Field(gt=0)
    max_steps: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_duration_seconds: float = Field(gt=0, allow_inf_nan=False)


class PublicTask(BaseModel):
    """Only fields used at this execution boundary are interpreted."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    upstream_id: str = Field(pattern=r"^instance_[a-zA-Z0-9_.-]+$")
    repository: str
    upstream_task_path: str
    upstream_task_tree_git_sha1: str = Field(pattern=r"^[a-f0-9]{40}$")
    base_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    docker_image_tag: str
    source_file_sha256: dict[str, str]
    budget: _Budget


class _Source(BaseModel):
    repository: Literal["https://github.com/scaleapi/SWE-bench_Pro-os"]
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")


class _Protocol(BaseModel):
    model: Literal["deepseek-flash"]
    temperature: Literal[0]
    thinking: Literal[False]
    repeats: Literal[1]
    agent_count: Literal[1]
    total_run_token_budget: int = Field(gt=0)
    cost_metrics: Literal[False]


class PublicSelection(BaseModel):
    """Validated frozen selection, separate from local fixture manifests."""

    schema_version: Literal["1.0"]
    kind: Literal["benchmark_selection"]
    name: str
    source: _Source
    run_protocol: _Protocol
    tasks: list[PublicTask] = Field(min_length=1)

    @property
    def token_budget(self) -> int:
        """Return the nominal budget shared by this complete sweep."""
        return self.run_protocol.total_run_token_budget

    @classmethod
    def from_file(cls, path: Path) -> PublicSelection:
        """Reject unsupported protocols and paths before any external action."""
        try:
            selection = cls.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            raise PublicStudyError("invalid public selection") from exc
        if len({task.id for task in selection.tasks}) != len(selection.tasks):
            raise PublicStudyError("duplicate task IDs")
        if len({task.upstream_id for task in selection.tasks}) != len(selection.tasks):
            raise PublicStudyError("duplicate upstream IDs")
        for task in selection.tasks:
            if task.upstream_task_path != "v2/tasks/" + task.upstream_id:
                raise PublicStudyError("invalid source task path")
            if task.docker_image_tag != _IMAGE_ROOT + task.upstream_id:
                raise PublicStudyError("invalid official image reference")
            if set(task.source_file_sha256) != {
                "instruction.md",
                "task.toml",
                "environment/Dockerfile",
            }:
                raise PublicStudyError("invalid source hash fields")
        return selection

    def verify_sources(self, root: Path) -> None:
        """Verify specs AND the complete verifier/reference tree before execution."""
        for task in self.tasks:
            directory = root / task.upstream_id
            try:
                for filename, expected in task.source_file_sha256.items():
                    actual = (
                        "sha256:" + hashlib.sha256((directory / filename).read_bytes()).hexdigest()
                    )
                    if actual != expected:
                        raise PublicStudyError("source hash mismatch: " + task.id)
                if _tree_hash(directory) != task.upstream_task_tree_git_sha1:
                    raise PublicStudyError("source tree mismatch: " + task.id)
            except OSError as exc:
                raise PublicStudyError("source files unavailable: " + task.id) from exc


def git_object_hash(kind: str, body: bytes) -> bytes:
    return hashlib.sha1(f"{kind} {len(body)}\0".encode() + body).digest()


def _tree_hash(directory: Path) -> str:
    entries = sorted(directory.iterdir(), key=lambda p: p.name + ("/" if p.is_dir() else ""))
    body = bytearray()
    for entry in entries:
        if entry.is_symlink():
            raise PublicStudyError("source symlinks are not supported")
        if entry.is_dir():
            mode, digest = "40000", bytes.fromhex(_tree_hash(entry))
        else:
            mode = "100755" if entry.stat().st_mode & 0o111 else "100644"
            digest = git_object_hash("blob", entry.read_bytes())
        body.extend(mode.encode() + b" " + entry.name.encode() + b"\0" + digest)
    return git_object_hash("tree", bytes(body)).hex()


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "mini-dsh-public-benchmark"})
    with urllib.request.urlopen(request, timeout=60) as response:
        body = cast("bytes", response.read(_MAX_DOWNLOAD + 1))
    if len(body) > _MAX_DOWNLOAD:
        raise PublicStudyError("source download exceeds size limit")
    return body


def prepare_sources(selection: PublicSelection, root: Path) -> None:
    """Fetch just the selected task trees from the pinned public commit."""
    commit_url = (
        "https://api.github.com/repos/scaleapi/SWE-bench_Pro-os/git/commits/"
        + selection.source.commit
    )
    commit = json.loads(download(commit_url))
    if commit.get("sha") != selection.source.commit:
        raise PublicStudyError("upstream commit mismatch")
    tree_url = (
        "https://api.github.com/repos/scaleapi/SWE-bench_Pro-os/git/trees/"
        + commit["tree"]["sha"]
        + "?recursive=1"
    )
    payload = json.loads(download(tree_url))
    if payload.get("sha") != commit["tree"]["sha"] or payload.get("truncated"):
        raise PublicStudyError("upstream source tree is incomplete or changed")
    entries = payload["tree"]
    for task in selection.tasks:
        upstream = next((e for e in entries if e["path"] == task.upstream_task_path), None)
        if upstream is None or upstream["sha"] != task.upstream_task_tree_git_sha1:
            raise PublicStudyError("upstream task tree mismatch: " + task.id)
        prefix = task.upstream_task_path + "/"
        for entry in entries:
            if entry["type"] != "blob" or not entry["path"].startswith(prefix):
                continue
            if entry["mode"] not in {"100644", "100755"}:
                raise PublicStudyError("unsupported source file mode")
            relative = Path(entry["path"][len(prefix) :])
            if relative.is_absolute() or ".." in relative.parts:
                raise PublicStudyError("unsafe upstream source path")
            destination = root / task.upstream_id / relative
            if destination.is_symlink() or not destination.resolve().is_relative_to(root.resolve()):
                raise PublicStudyError("source destination is a symlink")
            body = download(_RAW_ROOT + selection.source.commit + "/" + entry["path"])
            if git_object_hash("blob", body).hex() != entry["sha"]:
                raise PublicStudyError("downloaded source blob mismatch")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            destination.chmod(0o755 if entry["mode"] == "100755" else 0o644)
    selection.verify_sources(root)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


async def run_checked(
    host: ProcessRunner, argv: tuple[str, ...], timeout_seconds: float = 120
) -> str:
    try:
        result = await host.run(argv, timeout_seconds=timeout_seconds)
    except OSError as exc:
        raise PublicStudyError("Docker is unavailable; no model calls were started") from exc
    if result.exit_code or result.timed_out:
        raise PublicStudyError("Docker command failed: " + argv[1])
    return result.stdout


async def _repository(box: DockerSandbox, task: PublicTask) -> str:
    for directory in ("/app", "/testbed"):
        result = await box.execute(("git", "rev-parse", "HEAD"), cwd=directory)
        if result.exit_code == 0 and result.stdout.strip() == task.base_commit:
            return directory
    raise PublicStudyError("image initial commit mismatch: " + task.id)


async def verify_patch(  # noqa: PLR0913 - explicit grading boundary
    host: ProcessRunner, *, image: str, task: PublicTask, sources: Path, patch: Path, logs: Path
) -> bool:
    """Grade only a captured patch in a fresh offline official image."""
    await asyncio.to_thread(logs.mkdir, parents=True, exist_ok=True)
    async with DockerSandbox(host, image=image) as box:
        repository = await _repository(box, task)
        created = await box.execute(("mkdir", "-p", "/tests", "/logs/verifier"))
        if created.exit_code:
            raise PublicStudyError("cannot create verifier directories")
        await run_checked(
            host,
            (
                "docker",
                "cp",
                str(sources / task.upstream_id / "tests") + "/.",
                box.name + ":/tests",
            ),
        )
        if (await asyncio.to_thread(patch.stat)).st_size:
            await run_checked(host, ("docker", "cp", str(patch), box.name + ":/tmp/model.patch"))
            applied = await box.execute(
                ("git", "apply", "--binary", "/tmp/model.patch"), cwd=repository
            )
            if applied.exit_code or applied.timed_out:
                (logs / "patch-error.txt").write_text(applied.stderr, encoding="utf-8")
                return False
        result = await box.execute(("bash", "/tests/test.sh"), cwd=repository, timeout_seconds=3000)
        (logs / "stdout.txt").write_text(result.stdout, encoding="utf-8")
        (logs / "stderr.txt").write_text(result.stderr, encoding="utf-8")
        if not box.alive:
            return False
        await run_checked(host, ("docker", "cp", box.name + ":/logs/verifier/.", str(logs)))
        reward = logs / "reward.txt"
        return result.exit_code == 0 and reward.is_file() and reward.read_text().strip() == "1"


def _fingerprint(selection: PublicSelection, images: dict[str, str]) -> str:
    payload = selection.model_dump_json() + json.dumps(images, sort_keys=True)
    payload += Path(__file__).read_text()
    # Code hash locks the bridge implementation without importing dataset code into the loop.
    payload += (Path(__file__).parent.parent / "infrastructure/containers.py").read_text()
    return hashlib.sha256(payload.encode()).hexdigest()


async def check_environment(
    selection: PublicSelection,
    sources: Path,
    output: Path,
    host: ProcessRunner,
    *,
    image_mirror: str | None = None,
) -> dict[str, str]:
    """Resolve images and validate reference/empty patches before any model use."""
    selection.verify_sources(sources)
    await run_checked(host, ("docker", "version", "--format", "{{.Server.Version}}"))
    images: dict[str, str] = {}
    for task in selection.tasks:
        images[task.id] = await resolve_image(host, task, mirror=image_mirror)
        write_json(output / "images.json", images)
    gates: list[dict[str, object]] = []
    empty = output / "empty.patch"
    empty.write_text("", encoding="utf-8")
    for task in selection.tasks:
        gold = await verify_patch(
            host,
            image=images[task.id],
            task=task,
            sources=sources,
            patch=sources / task.upstream_id / "solution/gold_patch.diff",
            logs=output / "gates" / task.id / "reference",
        )
        nop = await verify_patch(
            host,
            image=images[task.id],
            task=task,
            sources=sources,
            patch=empty,
            logs=output / "gates" / task.id / "empty",
        )
        gates.append({"task_id": task.id, "reference_passed": gold, "empty_passed": nop})
        write_json(
            output / "gates.json", {"fingerprint": _fingerprint(selection, images), "tasks": gates}
        )
        if not gold or nop:
            raise PublicStudyError("reference/empty gate failed: " + task.id)
    return images


async def resolve_image(host: ProcessRunner, task: PublicTask, *, mirror: str | None = None) -> str:
    """Prefer an explicitly selected mirror, always using the upstream content digest."""
    if mirror is not None and re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", mirror) is None:
        raise PublicStudyError("image mirror must be a TLS registry hostname")
    raw = await run_checked(
        host, ("docker", "manifest", "inspect", "--verbose", task.docker_image_tag)
    )
    manifest = json.loads(raw)
    manifests = manifest if isinstance(manifest, list) else [manifest]
    descriptors = [entry.get("Descriptor") for entry in manifests if isinstance(entry, dict)]
    candidates = [
        descriptor
        for descriptor in descriptors
        if isinstance(descriptor, dict)
        and descriptor.get("platform") == {"architecture": "amd64", "os": "linux"}
    ]
    if len(candidates) != 1:
        raise PublicStudyError("expected exactly one upstream Linux/amd64 manifest")
    descriptor = candidates[0]
    if not isinstance(descriptor, dict):
        raise PublicStudyError("invalid upstream manifest descriptor")
    digest = descriptor.get("digest", "")
    if not isinstance(digest, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", digest) is None:
        raise PublicStudyError("invalid upstream manifest digest")
    canonical = _IMAGE_ROOT[:-1] + "@" + digest
    cached = await host.run(("docker", "image", "inspect", canonical))
    if not cached.exit_code and not cached.timed_out:
        _validate_image(cached.stdout, canonical)
        return canonical
    references = [canonical]
    if mirror is not None:
        references.insert(0, mirror + "/scaleapi/swe-bench_pro-v2@" + digest)
    for reference in references:
        try:
            await run_checked(
                host,
                ("docker", "pull", "--platform", "linux/amd64", reference),
                1800,
            )
            info = await run_checked(host, ("docker", "image", "inspect", reference))
            _validate_image(info, reference)
            return reference
        except PublicStudyError:
            if reference == canonical:
                raise
    raise PublicStudyError("image resolution failed")


def _validate_image(raw: str, reference: str) -> None:
    info = json.loads(raw)[0]
    if info["Architecture"] != "amd64" or info["Os"] != "linux":
        raise PublicStudyError("image architecture mismatch")
    if reference not in info.get("RepoDigests", []):
        raise PublicStudyError("image digest mismatch")


class UsageModel:
    def __init__(self, model: Model) -> None:
        self.model = model
        self.unknown_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            return await self.model.complete(request)
        except ModelCallError as exc:
            self.unknown_calls += int(exc.usage is None)
            raise
        except asyncio.CancelledError:
            # A deadline can cancel an already submitted request with no usage response.
            self.unknown_calls += 1
            raise
        except Exception:
            self.unknown_calls += 1
            raise


def summarize(rows: list[dict[str, object]], token_budget: int) -> dict[str, object]:
    """Keep all selected tasks and failure tokens in the score denominator."""
    tokens = [int(str(row.get("tokens", 0))) for row in rows]
    total = sum(tokens)
    successes = sum(row.get("success") is True for row in rows)
    return {
        "tasks": len(rows),
        "successes": successes,
        "success_rate": successes / len(rows) if rows else 0,
        "total_tokens": total,
        "mean_tokens": total / len(rows) if rows else 0,
        "p50_tokens": statistics.median(tokens) if tokens else 0,
        "tokens_per_success": total / successes if successes else None,
        "nominal_token_budget": token_budget,
        "token_overrun": max(0, total - token_budget),
        "unknown_usage_calls": sum(int(str(row.get("unknown_usage_calls", 0))) for row in rows),
    }


async def _capture_patch(box: DockerSandbox, repository: str, base_commit: str, path: Path) -> None:
    # Include new files and diff against the pinned base, even if the agent committed.
    added = await box.execute(("git", "add", "--intent-to-add", "--all"), cwd=repository)
    if added.exit_code:
        raise PublicStudyError("cannot capture new files")
    patch = await box.execute(
        ("git", "diff", "--binary", "--no-ext-diff", base_commit, "--"), cwd=repository
    )
    if patch.exit_code or patch.timed_out:
        raise PublicStudyError("patch capture failed or exceeded output limit")
    await asyncio.to_thread(path.write_text, patch.stdout, encoding="utf-8")


async def run_study(
    selection: PublicSelection,
    *,
    sources: Path,
    output: Path,
    host: ProcessRunner,
    model_factory: Callable[[], Model],
) -> dict[str, object]:
    """Run one sequential sweep; infrastructure gates precede model construction."""
    selection.verify_sources(sources)
    await run_checked(host, ("docker", "version", "--format", "{{.Server.Version}}"))
    try:
        images: dict[str, str] = json.loads((output / "images.json").read_text())
        gates = json.loads((output / "gates.json").read_text())
    except (OSError, ValueError) as exc:
        raise PublicStudyError("run check first to validate images and verifiers") from exc
    if gates["fingerprint"] != _fingerprint(selection, images) or {
        row["task_id"]
        for row in gates["tasks"]
        if row["reference_passed"] and not row["empty_passed"]
    } != {task.id for task in selection.tasks}:
        raise PublicStudyError("complete, matching reference/empty gates are required")
    if (output / "report.json").exists():
        raise PublicStudyError(
            "use a fresh output directory; existing results cannot be overwritten"
        )
    rows: list[dict[str, object]] = [
        {"task_id": task.id, "success": False, "tokens": 0, "status": "not_run"}
        for task in selection.tasks
    ]
    consumed = 0
    stopped = False
    for task, row in zip(selection.tasks, rows, strict=True):
        if not stopped and consumed < selection.token_budget:
            directory = output / "runs" / task.id
            directory.mkdir(parents=True, exist_ok=True)
            budget = AgentBudget(
                max_steps=task.budget.max_steps,
                max_tokens=min(task.budget.max_tokens, selection.token_budget - consumed),
                max_tool_calls=task.budget.max_tool_calls,
                max_duration_seconds=task.budget.max_duration_seconds,
            )
            model = UsageModel(model_factory())
            try:
                async with DockerSandbox(host, image=images[task.id]) as box:
                    repository = await _repository(box, task)
                    runner = box.runner(repository, CommandPolicy(_COMMANDS))
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
                                "You are a coding agent. Use shell argv arrays to work "
                                "in the repository. "
                                "Use python3 to read and atomically patch files, "
                                "or other allowlisted commands. "
                                "No shell string interpreter is available. Network is disabled. "
                                "Run relevant existing tests. Preserve Git history. "
                                "Finish with a brief answer."
                            ),
                        ),
                        components=RuntimeComponents(
                            runner=runner, tools=(ShellTool(runner), GitTool(runner))
                        ),
                    )
                    runtime.events.subscribe(
                        "*", JsonlTraceWriter(directory / "trace.jsonl", truncate_on_start=True)
                    )
                    goal = (sources / task.upstream_id / "instruction.md").read_text(
                        encoding="utf-8"
                    )
                    result = await runtime.agent.run(goal)
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
                        await _capture_patch(
                            box, repository, task.base_commit, directory / "model.patch"
                        )
                    else:
                        row["failure_type"] = "tool_timeout"
                if (directory / "model.patch").is_file():
                    # A partial patch from a budget-exhausted run is still independently graded.
                    row["success"] = await verify_patch(
                        host,
                        image=images[task.id],
                        task=task,
                        sources=sources,
                        patch=directory / "model.patch",
                        logs=directory / "verifier",
                    )
                    if not row["success"]:
                        row["failure_type"] = "verification_failed"
            except (OSError, PublicStudyError) as exc:
                row.update({"failure_type": "infrastructure_error", "failure_reason": str(exc)})
                stopped = True
        else:
            row["failure_type"] = "sweep_stopped" if stopped else "aggregate_budget_exhausted"
        report: dict[str, object] = {
            "schema_version": "1.0",
            "name": selection.name,
            "source_commit": selection.source.commit,
            "fingerprint": gates["fingerprint"],
            "model": selection.run_protocol.model,
            "tools": ["git", "shell"],
            "agent_network": "none",
            "verifier_network": "none",
            "aggregate": summarize(rows, selection.token_budget),
            "results": rows,
        }
        write_json(output / "report.json", report)
    return report


def make_provider_model(credentials_file: Path | None) -> Callable[[], Model]:
    environ = dict(os.environ)
    if credentials_file is not None:
        for line in credentials_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "DEEPSEEK_API_KEY":
                environ[key.strip()] = value.strip().strip("\"'")
    profile = resolve_profile("deepseek")
    secret = profile.api_key(environ)

    def factory() -> Model:
        return CredentialRedactingModel(
            build_provider_model(profile, environ=environ, max_attempts=1), secret
        )

    return factory


def main() -> None:
    """Expose separate preparation, infrastructure gate and model-run stages."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "check", "run"))
    parser.add_argument(
        "--selection", type=Path, default=Path("benchmarks/public_swe/v1/selection.json")
    )
    parser.add_argument(
        "--sources", type=Path, default=Path("benchmark-results/public-swe/sources")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("benchmark-results/public-swe/baseline")
    )
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument(
        "--image-mirror",
        default=os.environ.get("MINIDSH_IMAGE_MIRROR"),
        help=(
            "preferred TLS registry hostname for GHCR downloads; fallback preserves upstream digest"
        ),
    )
    args = parser.parse_args()
    try:
        selection = PublicSelection.from_file(args.selection)
        if args.action == "prepare":
            prepare_sources(selection, args.sources)
        elif args.action == "check":
            asyncio.run(
                check_environment(
                    selection,
                    args.sources,
                    args.output,
                    DockerHost(),
                    image_mirror=args.image_mirror,
                )
            )
        else:
            # Delay key loading until run_study has validated all infrastructure gates.
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
        parser.exit(1, "Public benchmark stopped: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
