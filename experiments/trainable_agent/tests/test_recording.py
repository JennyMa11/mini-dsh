"""Token sidecars are durable, private and cannot overwrite earlier calls."""

import json
import stat
from pathlib import Path

import pytest
from minidsh_rl.recording import FileRecordingSink


async def test_raw_tokens_written_once_with_private_permissions(tmp_path: Path) -> None:
    sink = FileRecordingSink(tmp_path)
    record: dict[str, object] = {"call_index": 0, "run_id": "run", "completion_token_ids": [9, 8]}
    await sink.append(record)
    path = tmp_path / "turn_000.json"
    assert json.loads(path.read_text()) == record
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        await sink.append(record)
    assert json.loads(path.read_text()) == record
