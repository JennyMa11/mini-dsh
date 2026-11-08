"""Statically check Mini-dsh layers; never import or execute the inspected project."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from importlib.util import resolve_name
from pathlib import Path

# This is a dependency graph, not a numeric stack. Execution adapters are peers.
_ALLOWED = {
    "metadata": {"metadata"},
    "domain": {"domain", "metadata"},
    "ports": {"domain", "ports", "metadata"},
    "application": {"domain", "ports", "application", "metadata"},
    "runtime": {"domain", "ports", "runtime", "metadata"},
    "extensions": {"domain", "ports", "runtime", "extensions", "tools", "metadata"},
    "infrastructure": {"infrastructure", "domain", "metadata"},
    "tools": {"domain", "ports", "runtime", "infrastructure", "tools", "metadata"},
    "context": {"domain", "ports", "infrastructure", "context", "metadata"},
    "adapters": {"domain", "ports", "runtime", "infrastructure", "tools", "adapters", "metadata"},
    "evaluation": {"domain", "evaluation", "metadata"},
}
_RUNTIME_LAYERS = set(_ALLOWED) - {"evaluation"}
_ALLOWED["composition"] = _RUNTIME_LAYERS | {"composition"}
_ALLOWED["benchmark"] = _RUNTIME_LAYERS | {"composition", "evaluation", "benchmark"}
_ALLOWED["presentation"] = set(_ALLOWED) | {"presentation"}
_COMPOSITION = {"composition", "configured_runtime", "config", "collaboration"}
_PRESENTATION = {"__main__", "cli", "commands", "interactive", "terminal", "work_profile", "gui"}
_ENTRYPOINTS = {"minidsh.cli", "minidsh.__main__", "minidsh.work_profile"}
_EXTENSIONS = {"skills", "memory", "memory_runner", "memory_maintenance", "verification"}
_RUNTIME_CHILD_INDEX = 2


@dataclass(frozen=True)
class Violation:
    """One source location that violates a declared architecture rule."""

    code: str
    path: str
    line: int
    detail: str


@dataclass(frozen=True)
class Import:
    """One explicit Python import, including imports inside functions and type guards."""

    source: str
    target: str
    line: int


def layer(module: str) -> str:
    """Classify every package module explicitly; unknown top-level modules fail."""
    parts = module.split(".")
    if len(parts) == 1:
        return "metadata"
    name = parts[1]
    if name in _COMPOSITION:
        return "composition"
    if name in _PRESENTATION:
        return "presentation"
    if name == "runtime" and len(parts) > _RUNTIME_CHILD_INDEX and parts[2] in _EXTENSIONS:
        return "extensions"
    return name if name in _ALLOWED else "unknown"


def imports(module: str, path: Path, tree: ast.Module, modules: set[str]) -> list[Import]:
    """Resolve absolute and relative imports without importing their targets."""
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: list[Import] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(Import(module, alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = node.module or ""
            if node.level:
                target = resolve_name("." * node.level + target, package)
            found.append(Import(module, target, node.lineno))
            found.extend(
                Import(module, f"{target}.{alias.name}", node.lineno)
                for alias in node.names
                if f"{target}.{alias.name}" in modules
            )
    return found


def cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Find strongly connected module groups, including same-layer cycles."""
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    active: set[str] = set()
    result: list[list[str]] = []

    def visit(node: str) -> None:
        indices[node] = low[node] = len(indices)
        stack.append(node)
        active.add(node)
        for target in sorted(graph[node]):
            if target not in indices:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in active:
                low[node] = min(low[node], indices[target])
        if low[node] != indices[node]:
            return
        group = []
        while True:
            member = stack.pop()
            active.remove(member)
            group.append(member)
            if member == node:
                break
        if len(group) > 1:
            result.append(sorted(group))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return sorted(result)


def inspect_project(root: Path) -> dict[str, object]:
    """Return a deterministic source inventory, import graph and rule violations."""
    source = root / "src/minidsh"
    paths: dict[str, Path] = {}
    for path in sorted(source.rglob("*.py")):
        parts = list(path.relative_to(root / "src").with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        paths[".".join(parts)] = path
    violations: list[Violation] = []
    if not paths:
        violations.append(Violation("missing_source", "src/minidsh", 0, "no Python modules found"))
    graph: dict[str, set[str]] = {module: set() for module in paths}
    edges: set[tuple[str, str]] = set()
    sizes: list[tuple[str, int]] = []
    source_hash = hashlib.sha256()
    for module, path in paths.items():
        relative = path.relative_to(root).as_posix()
        owner = layer(module)
        if owner == "unknown":
            violations.append(Violation("unclassified_module", relative, 0, module))
        raw = path.read_bytes()
        content = raw.decode("utf-8")
        source_hash.update(relative.encode("utf-8") + b"\0" + raw + b"\0")
        sizes.append((module, len(content.splitlines())))
        try:
            tree = ast.parse(content, filename=relative)
            dependencies = imports(module, path, tree, set(paths))
        except (SyntaxError, ImportError, ValueError) as exc:
            violations.append(
                Violation("syntax_error", relative, getattr(exc, "lineno", 0) or 0, str(exc))
            )
            continue
        for dependency in dependencies:
            target = dependency.target
            if not (target == "minidsh" or target.startswith("minidsh.")):
                if owner in {"domain", "ports", "application"} and (
                    target.split(".")[0] not in sys.stdlib_module_names | {"__future__"}
                ):
                    violations.append(
                        Violation("external_core_dependency", relative, dependency.line, target)
                    )
                continue
            if target in paths and target != module:
                graph[module].add(target)
            edges.add((module, target))
            # The evaluator consumes the same wire schema as the production reader.
            shared_schema = owner == "evaluation" and target == "minidsh.adapters.scoring_evidence"
            command_module = module == "minidsh.commands" or module.startswith("minidsh.commands.")
            entrypoint_dependency = command_module and target in _ENTRYPOINTS
            if entrypoint_dependency or (
                not shared_schema and layer(target) not in _ALLOWED.get(owner, set())
            ):
                violations.append(
                    Violation(
                        "forbidden_dependency", relative, dependency.line, f"{module} -> {target}"
                    )
                )
    groups = cycles(graph)
    return {
        "schema_version": "1.0",
        "generator": "uv run python scripts/check_architecture.py --json",
        "source_sha256": source_hash.hexdigest(),
        "checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "explicit Python imports in src/minidsh; no project execution",
        "module_count": len(paths),
        "import_count": len(edges),
        "layers": dict(sorted(Counter(layer(module) for module in paths).items())),
        "dependencies": [list(edge) for edge in sorted(edges)],
        "violations": [asdict(item) for item in violations],
        "cycles": groups,
        "hotspots": [
            {"module": module, "lines": lines}
            for module, lines in sorted(sizes, key=lambda item: (-item[1], item[0]))[:10]
        ],
    }


def main() -> int:
    """Print an audit and fail on rule violations or explicit import cycles."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--json", action="store_true", help="Print the full machine-readable inventory"
    )
    args = parser.parse_args()
    report = inspect_project(args.root.resolve())
    if args.json:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(
            f"Modules: {report['module_count']}; internal imports: {report['import_count']}\n"
        )
        summary = {key: report[key] for key in ("layers", "violations", "cycles")}
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return int(bool(report["violations"] or report["cycles"]))


if __name__ == "__main__":
    raise SystemExit(main())
