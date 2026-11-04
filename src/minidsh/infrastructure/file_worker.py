"""Standard-library-only file operations, also shipped as controlled container code."""

import ast
import difflib
import fnmatch
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

_SKIP = {".git", ".minidsh", ".venv", "node_modules", "dist", "build"}
_SENSITIVE = {
    ".env",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "credentials.toml",
    "id_rsa",
    "id_ed25519",
}
_MAX_FILE_BYTES = 256_000


def _resolve(root: Path, value: str) -> Path:
    if Path(value).is_absolute():
        raise ValueError("absolute paths are not allowed")
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("path escapes workspace")
    if (
        (
            ".minidsh" in path.relative_to(root).parts
            and "artifacts" not in path.relative_to(root).parts
        )
        or path.name.lower() in _SENSITIVE
        or path.name.startswith(".env.")
        or path.suffix.lower() in {".key", ".pem", ".p12"}
        or ".ssh" in path.relative_to(root).parts
    ):
        raise ValueError("sensitive file access denied")
    return path


def _read(path: Path) -> str:
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("file exceeds bounded worker input")
    return path.read_text(encoding="utf-8")


def _write(root: Path, path: Path, content: str) -> None:
    if any(part.lower() == ".minidsh" for part in path.relative_to(root).parts):
        raise ValueError("runtime metadata and artifacts are not model-writable")
    parent = path.parent.resolve(strict=True)
    if not parent.is_relative_to(root):
        raise ValueError("parent escapes workspace")
    descriptor, temporary = tempfile.mkstemp(prefix=".minidsh-", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _updated(original: str, arguments: dict[str, Any]) -> str:
    expected = cast("str", arguments["expected"])
    replacement = cast("str", arguments["replacement"])
    count = original.count(expected)
    if not expected or count == 0:
        raise ValueError("patch_mismatch")
    occurrence = arguments.get("occurrence")
    if count > 1 and occurrence is None:
        raise ValueError("patch_ambiguous")
    target = occurrence or 1
    if target > count:
        raise ValueError("patch_mismatch")
    position = -len(expected)
    for _ in range(target):
        position = original.find(expected, position + len(expected))
    return original[:position] + replacement + original[position + len(expected) :]


_SKILL_BYTES = 32 * 1024
_SKILL_COUNT = 32


def _skill_path(root: Path, value: str) -> Path:
    if not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("skill_path_invalid")
    try:
        path = _resolve(root, value)
        parts = {part.lower() for part in path.relative_to(root).parts}
        if parts & (_SENSITIVE | {".ssh", ".minidsh"}) or any(
            part.startswith(".env.") for part in parts
        ):
            raise ValueError("skill_source_denied")
        return path
    except (OSError, RuntimeError, ValueError):
        raise ValueError("skill_source_denied") from None


def _skill_bytes(root: Path, path: Path) -> bytes:
    """Anchor every canonical component; replacements cannot redirect an open outside root.

    This infrastructure adapter fails closed on platforms without no-follow dir-fd opens.
    Canonical internal symlinks are resolved before this point, then opened without links.
    """
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("skill_source_denied")
    parts = path.relative_to(root).parts
    if not parts or parts[-1] != "SKILL.md":
        raise ValueError("skill_source_denied")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, directory_flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(source, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("skill_source_denied")
            if info.st_size > _SKILL_BYTES:
                raise ValueError("skill_size_limit")
            return stream.read(_SKILL_BYTES + 1)
    finally:
        os.close(descriptor)


def _skill_document(root: Path, path: Path, expected: str | None = None) -> dict[str, str]:
    raw = _skill_bytes(root, path)
    if len(raw) > _SKILL_BYTES:
        raise ValueError("skill_size_limit")
    digest = hashlib.sha256(raw).hexdigest()
    if expected is not None and digest != expected:
        raise ValueError("skill_changed")
    try:
        content = raw.decode("utf-8")
    except UnicodeError:
        raise ValueError("skill_encoding_invalid") from None
    return {"path": path.relative_to(root).as_posix(), "sha256": digest, "content": content}


def _skill_walk_error(_error: OSError) -> None:
    raise ValueError("skill_source_denied")


def _skill_scan(root: Path, roots: list[str]) -> dict[str, list[dict[str, str]]]:
    paths: set[Path] = set()
    visited: set[Path] = set()
    for value in sorted(roots):
        directory = _skill_path(root, value)
        if not directory.is_dir():
            raise ValueError("skill_root_missing")
        for current, dirs, names in os.walk(directory, followlinks=True, onerror=_skill_walk_error):
            location = _skill_path(root, Path(current).relative_to(root).as_posix())
            if location in visited:
                raise ValueError("skill_source_cycle")
            visited.add(location)
            # Validate links even when their names would otherwise be skipped.
            for name in (*dirs, *names):
                child = Path(current) / name
                if child.is_symlink():
                    _skill_path(root, child.relative_to(root).as_posix())
            dirs[:] = sorted(name for name in dirs if name not in _SKIP)
            if "SKILL.md" in names:
                path = _skill_path(root, (Path(current) / "SKILL.md").relative_to(root).as_posix())
                if path in paths:
                    raise ValueError("skill_duplicate_source")
                paths.add(path)
                if len(paths) > _SKILL_COUNT:
                    raise ValueError("skill_count_limit")
    return {"documents": [_skill_document(root, path) for path in sorted(paths)]}


def _skill_dispatch(root: Path, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if operation == "skill_scan":
            roots = arguments.get("roots")
            if (
                set(arguments) != {"roots"}
                or not isinstance(roots, list)
                or any(not isinstance(p, str) for p in roots)
            ):
                raise ValueError("skill_roots_invalid")
            return _skill_scan(root, roots)
        if (
            set(arguments) != {"path", "expected_sha256"}
            or not isinstance(arguments["path"], str)
            or not isinstance(arguments["expected_sha256"], str)
        ):
            raise ValueError("skill_read_invalid")
        expected = arguments["expected_sha256"]
        if re.fullmatch(r"[a-f0-9]{64}", expected) is None:
            raise ValueError("skill_read_invalid")
        return _skill_document(root, _skill_path(root, arguments["path"]), expected)
    except OSError:
        raise ValueError("skill_source_denied") from None


def dispatch(root: Path, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:  # noqa: PLR0912, PLR0915 - closed operation protocol
    """Execute a closed set of file operations; arguments are data, never Python code."""
    root = root.resolve(strict=True)
    if operation in {"skill_scan", "skill_read"}:
        return _skill_dispatch(root, operation, arguments)
    path = _resolve(root, str(arguments.get("path", ".")))
    if operation == "prepare":
        tool = arguments["tool"]
        args = arguments["arguments"]
        target = _resolve(root, args["path"])
        if any(part.lower() == ".minidsh" for part in target.relative_to(root).parts):
            raise ValueError("runtime metadata and artifacts are not model-writable")
        old = _read(target) if target.exists() else ""
        if tool == "patch_file":
            new = _updated(old, args)
        elif tool == "write_file":
            if target.exists() and not args.get("overwrite", False):
                raise ValueError("file_exists")
            new = args["content"]
        else:
            raise ValueError("unknown prepare operation")
        preview = "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=args["path"],
                tofile=args["path"],
            )
        )
        return {"scope": str(target), "preview": preview}
    if operation == "read_file":
        lines = _read(path).splitlines()
        start = arguments.get("start_line", 1)
        end = arguments.get("end_line") or len(lines)
        if end < start:
            raise ValueError("invalid_range")
        content = "\n".join(lines[start - 1 : end])
        maximum = arguments.get("max_chars", 20_000)
        return {
            "path": arguments["path"],
            "start_line": start,
            "end_line": min(end, len(lines)),
            "content": content[:maximum],
            "truncated": len(content) > maximum,
        }
    if operation in {"write_file", "patch_file"}:
        if operation == "write_file":
            if path.exists() and not arguments.get("overwrite", False):
                raise ValueError("file_exists")
            content = arguments["content"]
        else:
            content = _updated(_read(path), arguments)
        _write(root, path, content)
        result = {"path": arguments["path"], "bytes_written": len(content.encode())}
        if operation == "patch_file":
            result["occurrence"] = arguments.get("occurrence") or 1
        return result
    if not path.exists():
        raise FileNotFoundError("file_not_found")
    candidates = (path,) if path.is_file() else sorted(path.rglob("*"))
    maximum = arguments.get("max_results", arguments.get("max_files", 100))
    output: list[Any] = []
    truncated = False
    for candidate in candidates:
        relative = candidate.relative_to(root)
        if not candidate.is_file() or any(part in _SKIP for part in relative.parts):
            continue
        if not fnmatch.fnmatch(candidate.name, arguments.get("glob", "*")):
            continue
        try:
            resolved = _resolve(root, relative.as_posix())
            if operation == "list_files":
                values: list[Any] = [resolved.relative_to(root).as_posix()]
            else:
                content = _read(resolved)
                if operation == "search":
                    values = [
                        {"path": relative.as_posix(), "line": number, "text": line[:500]}
                        for number, line in enumerate(content.splitlines(), 1)
                        if arguments["query"] in line
                    ]
                elif operation == "search_code":
                    count = content.count(arguments["query"])
                    values = [{"path": relative.as_posix(), "matches": count}] if count else []
                elif operation == "find_symbol":
                    if candidate.suffix != ".py":
                        continue
                    needle = arguments["name"].rsplit(".", 1)[-1].lower()
                    values = [
                        {
                            "path": relative.as_posix(),
                            "name": node.name,
                            "start_line": node.lineno,
                            "end_line": node.end_lineno,
                        }
                        for node in ast.walk(ast.parse(content))
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                        and needle in node.name.lower()
                    ]
                else:
                    raise ValueError("unknown file operation")
            for value in values:
                if len(output) >= maximum:
                    truncated = True
                    break
                output.append(value)
        except (OSError, UnicodeError, SyntaxError, ValueError):
            continue
        if truncated:
            break
    key = {
        "list_files": "files",
        "search": "matches",
        "search_code": "candidates",
        "find_symbol": "symbols",
    }.get(operation)
    if key is None:
        raise ValueError("unknown file operation")
    if operation == "search_code":
        output.sort(key=lambda value: (-value["matches"], value["path"]))
    return {key: output, "truncated": truncated}


def main() -> None:
    request = json.loads(sys.argv[1])
    payload: dict[str, Any]
    try:
        result = dispatch(Path("/workspace"), request["operation"], request["arguments"])
        payload = {"result": result, "error": None}
    except (OSError, UnicodeError, ValueError) as exc:
        payload = {"result": None, "error": str(exc)}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
