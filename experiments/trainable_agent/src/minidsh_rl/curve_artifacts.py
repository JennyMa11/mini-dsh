"""Shared controlled artifact fingerprints; no runtime or SDK dependency."""

from pathlib import Path

from minidsh_rl.learning_schedule import digest


def tree_hashes(root: Path, directory: Path) -> dict[str, str]:
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlinked control evidence")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = digest(path)
    return result


def verify_hashes(root: Path, files: dict[str, str]) -> None:
    for name, expected in files.items():
        path = root / name
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(root.resolve())
            or digest(path) != expected
        ):
            raise ValueError("confirmed evidence changed")
