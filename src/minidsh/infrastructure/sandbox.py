"""Workspace-confined file access with symlink-aware path checks."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_SENSITIVE_NAMES = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "credentials.toml",
        "id_rsa",
        "id_ed25519",
    }
)
_SENSITIVE_SUFFIXES = frozenset({".key", ".p12", ".pem"})


class SandboxViolationError(PermissionError):
    """Raised when a requested path escapes the configured workspace."""


class WorkspaceSandbox:
    """Resolve and mutate files only underneath one canonical workspace root."""

    def __init__(self, workspace: Path) -> None:
        self.root = workspace.resolve(strict=True)
        if not self.root.is_dir():
            msg = f"workspace is not a directory: {self.root}"
            raise ValueError(msg)

    def resolve(self, path: str, *, must_exist: bool = False) -> Path:
        """Resolve a workspace-relative path and reject traversal or symlink escapes."""
        requested = Path(path)
        if requested.is_absolute():
            msg = "absolute paths are not allowed"
            raise SandboxViolationError(msg)
        try:
            candidate = (self.root / requested).resolve(strict=must_exist)
        except OSError as exc:
            msg = f"cannot resolve path: {path}"
            raise SandboxViolationError(msg) from exc
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            msg = f"path escapes workspace: {path}"
            raise SandboxViolationError(msg) from exc
        return candidate

    def read_text(self, path: str) -> str:
        """Read UTF-8 text from a confined regular file."""
        resolved = self.resolve(path, must_exist=True)
        self.ensure_not_sensitive(resolved)
        if not resolved.is_file():
            msg = f"not a regular file: {path}"
            raise SandboxViolationError(msg)
        return resolved.read_text(encoding="utf-8")

    def ensure_not_sensitive(self, path: Path) -> None:
        """Deny common credential files before their contents can enter model context."""
        relative = path.relative_to(self.root)
        if (
            (
                ".minidsh" in tuple(part.lower() for part in relative.parts)
                and "artifacts" not in relative.parts
            )
            or any(part.lower() == ".ssh" for part in relative.parts)
            or path.name.lower() in _SENSITIVE_NAMES
            or path.name.lower() == "credentials.toml"
            or path.name.lower().startswith(".env.")
            or path.suffix.lower() in _SENSITIVE_SUFFIXES
        ):
            msg = f"sensitive file access denied: {relative.as_posix()}"
            raise SandboxViolationError(msg)

    def atomic_write(self, path: str, content: str) -> Path:
        """Atomically replace one confined file after validating its parent."""
        resolved = self.resolve(path)
        parent = resolved.parent.resolve(strict=True)
        try:
            parent.relative_to(self.root)
        except ValueError as exc:
            msg = f"parent escapes workspace: {path}"
            raise SandboxViolationError(msg) from exc
        descriptor, temporary_name = tempfile.mkstemp(prefix=".minidsh-", dir=parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(resolved)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return resolved

    def ensure_model_writable(self, path: Path) -> None:
        """Model tools cannot overwrite control-plane facts or original output artifacts."""
        self.ensure_not_sensitive(path)
        if any(part.lower() == ".minidsh" for part in path.relative_to(self.root).parts):
            raise SandboxViolationError("runtime metadata and artifacts are not model-writable")
