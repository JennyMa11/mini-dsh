"""Bounded repository views prepared through the authorized tool execution chain."""

import ast
import re
import time
from collections.abc import Callable

from minidsh.context.retrieval import RetrievedChunk
from minidsh.domain import ToolCall, ToolObservation
from minidsh.ports import ToolExecutor

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_MAX_READS = 24


class SnapshotRetriever:
    def __init__(
        self, tools: ToolExecutor, *, symbols: bool = False, exclude_skills: bool = False
    ) -> None:
        self._tools = tools
        self._symbols = symbols
        self._exclude_skills = exclude_skills
        self._snapshot: dict[str, str] = {}

    async def prepare(
        self,
        task: str,
        run_id: str,
        step_id: int,
        max_calls: int,
        account: Callable[[int, float], None] | None = None,
    ) -> tuple[int, float]:
        """Every snapshot read consumes a tool opportunity and produces normal trace events."""
        self._snapshot = {}
        if max_calls <= 0:
            return 0, 0.0
        listed = await self._execute(
            ToolCall(f"context-map-{step_id}", "list_files", {"max_results": 80}),
            run_id,
            step_id,
            account,
        )
        duration = listed.duration_ms
        result = listed.result
        raw_paths = result.get("files") if isinstance(result, dict) else None
        if not isinstance(raw_paths, list):
            return 1, duration
        terms = {term.lower() for term in _WORD.findall(task)}
        paths = [path for path in raw_paths if isinstance(path, str)]
        if self._exclude_skills:
            paths = [path for path in paths if path.rsplit("/", 1)[-1] != "SKILL.md"]
        if self._symbols:
            paths = [path for path in paths if path.endswith(".py")]
        paths.sort(key=lambda path: (-sum(term in path.lower() for term in terms), path))
        calls = 1
        for index, path in enumerate(paths[: min(_MAX_READS, max_calls - 1)]):
            observed = await self._execute(
                ToolCall(
                    f"context-read-{step_id}-{index}",
                    "read_file",
                    {"path": path, "max_chars": 4000},
                ),
                run_id,
                step_id,
                account,
            )
            calls += 1
            duration += observed.duration_ms
            data = observed.result
            content = data.get("content") if isinstance(data, dict) else None
            if isinstance(content, str):
                self._snapshot[path] = content
        return calls, duration

    async def _execute(
        self,
        call: ToolCall,
        run_id: str,
        step_id: int,
        account: Callable[[int, float], None] | None,
    ) -> ToolObservation:
        if account:
            account(1, 0.0)
        started = time.monotonic()
        observed: ToolObservation | None = None
        try:
            observed = await self._tools.execute(call, run_id, step_id)
            return observed
        finally:
            if account:
                account(
                    0, observed.duration_ms if observed else (time.monotonic() - started) * 1000
                )

    def search(self, query: str, *, limit: int) -> tuple[RetrievedChunk, ...]:
        terms = {term.lower() for term in _WORD.findall(query)}
        matches: list[RetrievedChunk] = []
        for path, text in self._snapshot.items():
            if self._symbols:
                try:
                    tree = ast.parse(text)
                except SyntaxError:
                    continue
                lines = text.splitlines()
                for node in ast.walk(tree):
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        score = sum(term in node.name.lower() for term in terms)
                        if score:
                            matches.append(
                                RetrievedChunk(
                                    path,
                                    "\n".join(lines[node.lineno - 1 : node.end_lineno]),
                                    float(score + 2),
                                    node.lineno,
                                )
                            )
            else:
                score = sum(text.lower().count(term) for term in terms)
                score += 2 * sum(term in path.lower() for term in terms)
                if score:
                    matches.append(RetrievedChunk(path, text, float(score)))
        matches.sort(key=lambda item: (-item.score, item.path, item.start_line))
        return tuple(matches[:limit])
