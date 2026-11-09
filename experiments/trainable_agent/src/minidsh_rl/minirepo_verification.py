"""Sealed mini-repo checking and pure project-module candidate restrictions."""

import ast
import asyncio
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Literal

from minidsh.infrastructure.containers import ContainerResources, DockerSandbox
from minidsh.infrastructure.process import ProcessRunner
from minidsh_rl.minirepo_tasks import MiniTask
from minidsh_rl.task_rules import _SAFE_CALLS, _SAFE_METHODS
from minidsh_rl.verification import Verdict

IMAGE = (
    "python:3.12-slim-bookworm@sha256:"
    "e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337"
)


CurvePolicy = Literal["legacy", "curve_pure_v1", "curve_pure_v2"]

_POLICY_EXTRA_METHODS: dict[CurvePolicy, frozenset[str]] = {
    "legacy": frozenset(),
    "curve_pure_v1": frozenset({"issubset"}),
    "curve_pure_v2": frozenset({"issubset", "lstrip", "replace", "rstrip"}),
}


def candidate_rejection_reason(  # noqa: PLR0911, PLR0912 - explicit policy branches
    task: MiniTask,
    files: dict[str, str],
    *,
    policy: CurvePolicy = "legacy",
) -> dict[str, object] | None:
    """Return the first failing policy branch, or None when the candidate is allowed."""
    if policy not in _POLICY_EXTRA_METHODS:
        raise ValueError("unknown candidate policy")
    methods = _SAFE_METHODS | _POLICY_EXTRA_METHODS[policy]
    if set(files) != set(task.writable):
        return {
            "reason": "file_set_mismatch",
            "missing": sorted(set(task.writable) - set(files)),
            "extra": sorted(set(files) - set(task.writable)),
        }
    try:
        trees = {name[:-3]: ast.parse(source) for name, source in files.items()}
    except (SyntaxError, ValueError, RecursionError) as error:
        return {"reason": "syntax_error", "detail": type(error).__name__}
    functions = {
        module: {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        for module, tree in trees.items()
    }
    for module, tree in trees.items():
        names = set(_SAFE_CALLS)
        names.update(n.name for n in tree.body if isinstance(n, ast.FunctionDef))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                if node.level or node.module not in functions:
                    return {
                        "reason": "import_module",
                        "module": module,
                        "line": node.lineno,
                        "name": node.module,
                    }
                for alias in node.names:
                    if alias.name not in functions[node.module]:
                        return {
                            "reason": "import_name",
                            "module": module,
                            "line": node.lineno,
                            "name": alias.name,
                        }
                    if (alias.asname or alias.name).startswith("__"):
                        return {
                            "reason": "import_dunder",
                            "module": module,
                            "line": node.lineno,
                            "name": alias.asname or alias.name,
                        }
                    names.add(alias.asname or alias.name)
            elif not (
                isinstance(node, ast.FunctionDef)
                or (
                    isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                )
            ):
                return {
                    "reason": "top_level_statement",
                    "module": module,
                    "line": node.lineno,
                    "name": type(node).__name__,
                }
        for element in ast.walk(tree):
            if isinstance(element, ast.ImportFrom) and element not in tree.body:
                return {"reason": "nested_import", "module": module, "line": element.lineno}
            if isinstance(
                element, (ast.Import, ast.ClassDef, ast.AsyncFunctionDef, ast.Global, ast.Nonlocal)
            ):
                return {
                    "reason": "forbidden_node",
                    "module": module,
                    "line": element.lineno,
                    "name": type(element).__name__,
                }
            if isinstance(element, ast.Name) and element.id.startswith("__"):
                return {
                    "reason": "dunder_name",
                    "module": module,
                    "line": element.lineno,
                    "name": element.id,
                }
            if isinstance(element, ast.Attribute) and element.attr not in methods:
                return {
                    "reason": "unsupported_method",
                    "module": module,
                    "line": element.lineno,
                    "name": element.attr,
                }
            if isinstance(element, ast.Call):
                if isinstance(element.func, ast.Name) and element.func.id not in names:
                    return {
                        "reason": "unknown_call",
                        "module": module,
                        "line": element.lineno,
                        "name": element.func.id,
                    }
                if not isinstance(element.func, (ast.Name, ast.Attribute)):
                    return {
                        "reason": "invalid_call_target",
                        "module": module,
                        "line": element.lineno,
                        "name": type(element.func).__name__,
                    }
    return None


def candidate_allowed(
    task: MiniTask, files: dict[str, str], *, policy: CurvePolicy = "legacy"
) -> bool:
    """Only pure functions, literal module docstrings and safe project imports."""
    return candidate_rejection_reason(task, files, policy=policy) is None


async def seed(box: DockerSandbox, files: dict[str, str]) -> None:
    """Seed fixed flat paths through JSON data, without code interpolation."""
    if any("/" in name or name.startswith(".") for name in files):
        raise ValueError("invalid fixed fixture path")
    code = (
        "import json,sys;from pathlib import Path;root=Path('/app');root.mkdir(exist_ok=True);"
        "[(root/name).write_text(text) for name,text in "
        "json.loads(sys.argv[1]).items()]"
    )
    result = await box.execute(("python3", "-c", code, json.dumps(files)))
    if result.exit_code or result.timed_out:
        raise ValueError("fixture seeding failed")


async def verify(  # noqa: PLR0913 - independent container verifier boundary
    host: ProcessRunner,
    task: MiniTask,
    candidate: dict[str, str],
    image: str,
    output: Path,
    *,
    checks: Literal["private", "public"] = "private",
    policy: CurvePolicy = "legacy",
) -> Verdict:
    """Fresh container; hidden checks are never placed in an agent-accessible box."""
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    began = time.monotonic()
    rejection = candidate_rejection_reason(task, candidate, policy=policy)
    passed = rejection is None
    (output / "policy.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "policy": policy,
                "allowed": passed,
                "reason": rejection,
            },
            indent=2,
        )
    )
    exit_code, timed_out, stdout, stderr = -1, False, "", "candidate_policy_rejected"
    if passed:
        marker = "verified_" + secrets.token_hex(16)
        checker = (
            "from service import solve\n" + task.hidden_checks
            if checks == "private"
            else dict(task.files)["public_tests.py"]
        )
        checker += f"\nprint({marker!r})\n"
        async with DockerSandbox(
            host,
            image=image,
            resources=ContainerResources(
                platform="linux/arm64",
                cpus=1,
                memory_mb=256,
            ),
        ) as box:
            await seed(box, candidate | {"verify_private.py": checker})
            result = await box.execute(
                ("python3", "-B", "verify_private.py"), cwd="/app", timeout_seconds=8
            )
            exit_code, timed_out = result.exit_code, result.timed_out
            stdout, stderr = result.stdout, result.stderr
            passed = exit_code == 0 and not timed_out and marker in stdout.splitlines()
    verdict = Verdict(passed, exit_code, timed_out, (time.monotonic() - began) * 1000)
    (output / "stdout.txt").write_text(stdout)
    (output / "stderr.txt").write_text(stderr)
    (output / "result.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case_id": task.case_id,
                "criterion_version": "minirepo_dev_v1",
                "passed": passed,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "duration_ms": verdict.duration_ms,
                "candidate_sha256": hashlib.sha256(
                    json.dumps(candidate, sort_keys=True).encode()
                ).hexdigest(),
            },
            indent=2,
        )
    )
    return verdict
