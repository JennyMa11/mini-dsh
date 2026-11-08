"""Private immutable Token artifacts, stored independently from redacted Trace."""

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile


@dataclass(frozen=True, slots=True)
class FileRecordingSink:
    """One fresh run directory; publish each complete call atomically, once."""

    directory: Path

    async def append(self, record: dict[str, object]) -> None:
        await asyncio.to_thread(self._append, record)

    def _append(self, record: dict[str, object]) -> None:
        index = record.get("call_index")
        if type(index) is not int or index < 0:
            raise ValueError("invalid call index")
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"turn_{index:03}.json"
        with NamedTemporaryFile(mode="w", dir=self.directory, delete=False) as file:
            temporary = Path(file.name)
            try:
                file.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                file.flush()
                os.fsync(file.fileno())
                # Unlike replace(), link() refuses duplicate publication, including races.
                os.link(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
