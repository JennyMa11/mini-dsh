"""Controlled skill file operations: real temporary files, no network or shell."""

import hashlib
import os
from pathlib import Path

import pytest

from minidsh.adapters.file_backend import LocalFileBackend
from minidsh.infrastructure import file_worker
from minidsh.infrastructure.file_worker import dispatch
from minidsh.tools.runtime import ToolError

pytestmark = pytest.mark.integration


def skill(root: Path, relative: str = "skills/a/SKILL.md") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"---\r\nname: bug_fix\r\ndescription: Fix a bug\r\n---\r\nBody\r\n")
    return path


@pytest.mark.asyncio
async def test_skill_backend_preserves_raw_bytes_and_canonical_order(tmp_path: Path) -> None:
    a = skill(tmp_path, "skills/a/SKILL.md")
    skill(tmp_path, "skills/z/SKILL.md")
    backend = LocalFileBackend((), workspace=tmp_path)
    result = await backend.invoke("skill_scan", {"roots": ["skills"]})
    assert isinstance(result, dict)
    documents = result["documents"]
    assert isinstance(documents, list)
    assert [d["path"] for d in documents if isinstance(d, dict)] == [
        "skills/a/SKILL.md",
        "skills/z/SKILL.md",
    ]
    first = documents[0]
    assert isinstance(first, dict)
    assert first["content"] == a.read_bytes().decode()
    assert first["sha256"] == hashlib.sha256(a.read_bytes()).hexdigest()
    loaded = await backend.invoke(
        "skill_read", {"path": "skills/a/SKILL.md", "expected_sha256": first["sha256"]}
    )
    assert loaded == first


@pytest.mark.parametrize("source", ["../outside", "/tmp", "missing"])
def test_invalid_or_missing_roots_are_explicit_errors(tmp_path: Path, source: str) -> None:
    with pytest.raises(ValueError, match="skill_"):
        dispatch(tmp_path, "skill_scan", {"roots": [source]})


def test_changed_file_is_rejected_by_the_controlled_read(tmp_path: Path) -> None:
    path = skill(tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text("changed")
    with pytest.raises(ValueError, match="skill_changed"):
        dispatch(tmp_path, "skill_read", {"path": "skills/a/SKILL.md", "expected_sha256": digest})


@pytest.mark.parametrize("kind", ["file", "directory", "sensitive", "cycle"])
def test_symlink_escape_sensitive_target_and_cycles_reject_whole_scan(
    tmp_path: Path, kind: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = skill(workspace)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = skill(outside)
    if kind == "file":
        source.unlink()
        source.symlink_to(target)
    elif kind == "directory":
        (workspace / "skills/escape").symlink_to(outside, target_is_directory=True)
    elif kind == "sensitive":
        secret = workspace / ".env"
        secret.write_text("fake_sensitive_marker")
        source.unlink()
        source.symlink_to(secret)
    else:
        (workspace / "skills/a/loop").symlink_to(workspace / "skills", target_is_directory=True)
    with pytest.raises(ValueError, match="skill_") as error:
        dispatch(workspace, "skill_scan", {"roots": ["skills"]})
    assert "fake_sensitive_marker" not in str(error.value)


def test_internal_symlink_has_a_fixed_canonical_source(tmp_path: Path) -> None:
    original = skill(tmp_path, "sources/a/SKILL.md")
    folder = tmp_path / "skills"
    folder.mkdir()
    (folder / "SKILL.md").symlink_to(original)
    result = dispatch(tmp_path, "skill_scan", {"roots": ["skills"]})
    assert result["documents"][0]["path"] == "sources/a/SKILL.md"


@pytest.mark.parametrize("case", ["size", "encoding", "count"])
def test_file_bounds_are_checked_before_returning_any_catalog(tmp_path: Path, case: str) -> None:
    path = skill(tmp_path)
    if case == "size":
        path.write_bytes(b"x" * 32769)
    elif case == "encoding":
        path.write_bytes(b"\xff")
    else:
        for index in range(33):
            skill(tmp_path, f"skills/s{index}/SKILL.md")
    with pytest.raises(ValueError, match="skill_"):
        dispatch(tmp_path, "skill_scan", {"roots": ["skills"]})


@pytest.mark.asyncio
async def test_unbound_local_backend_does_not_gain_filesystem_access() -> None:
    backend = LocalFileBackend(())
    with pytest.raises(ToolError, match="skill_source_unavailable"):
        await backend.invoke("skill_scan", {"roots": ["."]})


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_swap_after_resolution_does_not_read_an_outside_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = skill(workspace)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = skill(outside)
    target.write_text("OUTSIDE_SOURCE_CANARY")
    original = file_worker._skill_document

    def swapped(root: Path, path: Path, expected: str | None = None) -> dict[str, str]:
        if kind == "file":
            source.unlink()
            source.symlink_to(target)
        else:
            source.parent.rename(workspace / "saved")
            source.parent.symlink_to(target.parent, target_is_directory=True)
        return original(root, path, expected)

    monkeypatch.setattr(file_worker, "_skill_document", swapped)
    with pytest.raises(ValueError, match="skill_") as error:
        dispatch(workspace, "skill_scan", {"roots": ["skills"]})
    assert "OUTSIDE_SOURCE_CANARY" not in str(error.value)
    assert target.read_text() == "OUTSIDE_SOURCE_CANARY"


@pytest.mark.parametrize(
    "relative", [".ENV.private/SKILL.md", ".SSH/nested/SKILL.md", ".MINIDSH/private/SKILL.md"]
)
def test_casefolded_sensitive_source_components_are_denied(tmp_path: Path, relative: str) -> None:
    path = skill(tmp_path, relative)
    with pytest.raises(ValueError, match="skill_"):
        dispatch(tmp_path, "skill_scan", {"roots": [path.parent.relative_to(tmp_path).as_posix()]})


def test_missing_nofollow_capability_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill(tmp_path)
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(ValueError, match="skill_source_denied"):
        dispatch(tmp_path, "skill_scan", {"roots": ["skills"]})


def test_worker_file_listing_canonicalizes_aliases_before_context_filtering(tmp_path: Path) -> None:
    source = skill(tmp_path)
    (tmp_path / "guide.md").symlink_to(source)
    listed = dispatch(tmp_path, "list_files", {"path": ".", "max_results": 80})
    assert "guide.md" not in listed["files"]
    assert set(listed["files"]) == {"skills/a/SKILL.md"}
