"""Trusted program sent to Docker as code; model arguments arrive only as JSON data."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_BYTES = 65536
READ_LIMIT = 5000
MATCH_LIMIT = 20
OUTPUT_LIMIT = 4000
MAX_READ_FILES = 4


def main() -> None:  # noqa: PLR0912, PLR0915 - isolated fixed tool dispatch
    request = json.loads(sys.argv[1])
    root = Path("/app")
    operation, data = request["operation"], request["data"]
    allowed, writable = set(request["allowed"]), set(request["writable"])

    def resolve(name: str, *, write: bool = False) -> Path:
        if name not in (writable if write else allowed):
            raise ValueError("path_denied")
        path = root / name
        if path.is_symlink() or path.resolve().parent != root:
            raise ValueError("path_denied")
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("oversized_file")
        return path

    result: dict[str, object]
    if operation == "read":
        path = resolve(data["path"])
        text = path.read_text()
        result = {"path": data["path"], "content": text[:5000], "truncated": len(text) > READ_LIMIT}
    elif operation == "read_many":
        names = data.get("paths")
        path_limit = request.get("batch_path_limit", MAX_READ_FILES)
        if type(path_limit) is not int or path_limit not in (4, 8):
            raise ValueError("invalid_batch_limit")
        if (
            not isinstance(names, list)
            or not 1 <= len(names) <= path_limit
            or any(not isinstance(name, str) for name in names)
            or len(set(names)) != len(names)
        ):
            raise ValueError("invalid_arguments")
        paths = [resolve(name) for name in names]
        remaining = READ_LIMIT
        files = []
        for name, path in zip(names, paths, strict=True):
            text = path.read_text()
            files.append(
                {"path": name, "content": text[:remaining], "truncated": len(text) > remaining}
            )
            remaining -= min(remaining, len(text))
        result = {
            "files": files,
            "returned_characters": READ_LIMIT - remaining,
            "truncated": any(item["truncated"] for item in files),
        }
    elif operation == "search":
        query = data["query"]
        matches = []
        for name in sorted(allowed):
            for index, line in enumerate(resolve(name).read_text().splitlines(), 1):
                if query in line:
                    matches.append({"path": name, "line": index, "text": line[:300]})
        result = {"matches": matches[:20], "truncated": len(matches) > MATCH_LIMIT}
    elif operation == "capture":
        actual = {p.name for p in root.iterdir() if p.name != "__pycache__"}
        if actual != allowed:
            raise ValueError("forbidden_workspace_change")
        result = {"files": {name: resolve(name).read_text() for name in sorted(allowed)}}
    elif operation == "patch":
        prepared: dict[str, str] = {}
        originals: dict[str, str] = {}
        for edit in data["edits"]:
            path = resolve(edit["path"], write=True)
            original = prepared.get(edit["path"], path.read_text())
            originals.setdefault(edit["path"], path.read_text())
            if edit["old"] == edit["new"] or original.count(edit["old"]) != 1:
                raise ValueError("patch_mismatch")
            updated = original.replace(edit["old"], edit["new"], 1)
            if len(updated.encode()) > MAX_BYTES:
                raise ValueError("oversized_file")
            prepared[edit["path"]] = updated
        staged: list[str] = []
        try:
            for text in prepared.values():
                with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False) as stream:
                    staged.append(stream.name)
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
            for temporary, name in zip(staged, prepared, strict=True):
                Path(temporary).replace(root / name)
        except OSError:
            for name, original in originals.items():
                (root / name).write_text(original)
            raise
        finally:
            for temporary in staged:
                Path(temporary).unlink(missing_ok=True)
        result = {"changed": list(prepared)}
    elif operation == "test":
        completed = subprocess.run(
            [sys.executable, "-B", "public_tests.py"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        result = {
            "exit_code": completed.returncode,
            "stdout": completed.stdout[:4000],
            "stderr": completed.stderr[:4000],
            "truncated": len(completed.stdout) > OUTPUT_LIMIT
            or len(completed.stderr) > OUTPUT_LIMIT,
        }
    else:
        raise ValueError("unknown_operation")
    sys.stdout.write(json.dumps({"ok": True, "result": result}) + "\n")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        sys.stdout.write(json.dumps({"ok": False, "error": str(error)[:200]}) + "\n")
