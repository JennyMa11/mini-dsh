"""Offline replay must neither trust arbitrary archive paths nor changed sources."""

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from minidsh_rl.replay_frozen import materialize


def archive(root: Path, name: str, content: bytes) -> None:
    with tarfile.open(root / "source.tar.gz", "w:gz") as handle:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        handle.addfile(member, io.BytesIO(content))


def test_exact_manifest_files_are_materialized(tmp_path: Path) -> None:
    content = b"original source\n"
    (tmp_path / "source.json").write_text(
        json.dumps({"src/module.py": hashlib.sha256(content).hexdigest()})
    )
    archive(tmp_path, "src/module.py", content)
    output = tmp_path / "extracted"
    materialize(tmp_path, output)
    assert (output / "src/module.py").read_bytes() == content


@pytest.mark.parametrize("name", ["../outside.py", "/outside.py", "unlisted.py"])
def test_unsafe_or_unlisted_archive_member_is_rejected(tmp_path: Path, name: str) -> None:
    (tmp_path / "source.json").write_text("{}")
    archive(tmp_path, name, b"anything")
    with pytest.raises(ValueError, match="unsafe member"):
        materialize(tmp_path, tmp_path / "extracted")


def test_changed_member_does_not_pass_manifest(tmp_path: Path) -> None:
    (tmp_path / "source.json").write_text(json.dumps({"src/module.py": "0" * 64}))
    archive(tmp_path, "src/module.py", b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        materialize(tmp_path, tmp_path / "extracted")
