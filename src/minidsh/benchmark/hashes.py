"""Content-addressed frozen benchmark trees."""

from __future__ import annotations

import hashlib
from pathlib import Path


def frozen_tree_digest(root: Path) -> str:
    """Hash relative paths and bytes; reject symlinks and ignore bytecode caches."""
    if not root.is_dir():
        raise ValueError(f"missing frozen tree: {root}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError(f"symlink in frozen tree: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode()
        body = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return f"sha256:{digest.hexdigest()}"
