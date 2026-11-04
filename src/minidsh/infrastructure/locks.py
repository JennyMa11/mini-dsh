"""Nonblocking platform-specific advisory locks; process death releases ownership."""

import importlib
import os
from pathlib import Path
from typing import BinaryIO


class FileLease:
    def __init__(self, path: Path) -> None:
        self._handle: BinaryIO = path.open("a+b")
        try:
            if os.name == "posix":
                module = importlib.import_module("fcntl")
                module.flock(self._handle.fileno(), module.LOCK_EX | module.LOCK_NB)
            else:
                module = importlib.import_module("msvcrt")
                self._handle.seek(0)
                if not self._handle.read(1):
                    self._handle.write(b"\0")
                    self._handle.flush()
                self._handle.seek(0)
                module.locking(self._handle.fileno(), module.LK_NBLCK, 1)
        except OSError as exc:
            self._handle.close()
            raise ValueError("session is busy in another writer") from exc

    def close(self) -> None:
        """Close is idempotent and releases the OS lease."""
        self._handle.close()
