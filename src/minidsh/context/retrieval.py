"""Small local retrieval adapters with no vector-database dependency."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from minidsh.infrastructure import WorkspaceSandbox

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_DEFAULT_SUFFIXES = frozenset(
    {".c", ".cpp", ".go", ".java", ".js", ".json", ".md", ".py", ".rs", ".toml", ".ts"}
)
_SKIPPED_PARTS = frozenset({".git", ".minidsh", ".venv", "node_modules", "dist", "build"})


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One ranked repository excerpt."""

    path: str
    content: str
    score: float
    start_line: int = 1


class RepositoryRetriever:
    """Rank bounded text files by lexical overlap with a task."""

    def __init__(
        self,
        workspace: Path,
        *,
        suffixes: frozenset[str] = _DEFAULT_SUFFIXES,
        max_file_bytes: int = 256_000,
        max_chunk_chars: int = 4_000,
    ) -> None:
        self._workspace = workspace.resolve(strict=True)
        self._sandbox = WorkspaceSandbox(self._workspace)
        self._suffixes = suffixes
        self._max_file_bytes = max_file_bytes
        self._max_chunk_chars = max_chunk_chars

    def search(self, query: str, *, limit: int) -> tuple[RetrievedChunk, ...]:
        terms = {match.group().lower() for match in _WORD.finditer(query)}
        if not terms or limit <= 0:
            return ()
        matches: list[RetrievedChunk] = []
        for path in self._files():
            try:
                if path.stat().st_size > self._max_file_bytes:
                    continue
                text = self._sandbox.read_text(path.relative_to(self._workspace).as_posix())
            except (OSError, UnicodeError):
                continue
            lowered = text.lower()
            score = float(sum(lowered.count(term) for term in terms))
            path_terms = path.relative_to(self._workspace).as_posix().lower()
            score += 2.0 * sum(term in path_terms for term in terms)
            if score <= 0:
                continue
            matches.append(
                RetrievedChunk(
                    path.relative_to(self._workspace).as_posix(),
                    text[: self._max_chunk_chars],
                    score,
                )
            )
        matches.sort(key=lambda item: (-item.score, item.path))
        return tuple(matches[:limit])

    def _files(self) -> tuple[Path, ...]:
        return tuple(
            path
            for path in self._workspace.rglob("*")
            if path.is_file()
            and path.suffix.lower() in self._suffixes
            and not any(part in _SKIPPED_PARTS for part in path.relative_to(self._workspace).parts)
        )


class PythonSymbolRetriever:
    """Retrieve Python functions and classes by symbol/task term overlap."""

    def __init__(self, workspace: Path, *, max_chunk_chars: int = 4_000) -> None:
        self._workspace = workspace.resolve(strict=True)
        self._sandbox = WorkspaceSandbox(self._workspace)
        self._max_chunk_chars = max_chunk_chars

    def search(self, query: str, *, limit: int) -> tuple[RetrievedChunk, ...]:
        terms = {match.group().lower() for match in _WORD.finditer(query)}
        if not terms or limit <= 0:
            return ()
        matches: list[RetrievedChunk] = []
        for path in self._workspace.rglob("*.py"):
            relative = path.relative_to(self._workspace)
            if any(part in _SKIPPED_PARTS for part in relative.parts):
                continue
            try:
                text = self._sandbox.read_text(relative.as_posix())
                tree = ast.parse(text)
            except (OSError, UnicodeError, SyntaxError):
                continue
            lines = text.splitlines()
            for node in ast.walk(tree):
                if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                score = float(sum(term in node.name.lower() for term in terms))
                if score <= 0:
                    continue
                end_line = getattr(node, "end_lineno", node.lineno)
                content = "\n".join(lines[node.lineno - 1 : end_line])[: self._max_chunk_chars]
                matches.append(
                    RetrievedChunk(relative.as_posix(), content, score + 2.0, node.lineno)
                )
        matches.sort(key=lambda item: (-item.score, item.path, item.start_line))
        return tuple(matches[:limit])
