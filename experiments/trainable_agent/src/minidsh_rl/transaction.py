"""Durable update phases; unknown optimizer submissions are never replayable."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

_TRANSITIONS = {
    "prepared": {"backward_submitted", "skipped"},
    "backward_submitted": {"backward_confirmed"},
    "backward_confirmed": {"optim_submitted"},
    "optim_submitted": {"optim_confirmed"},
    "optim_confirmed": {"checkpointed"},
    "checkpointed": set(),
    "skipped": set(),
}


@dataclass(slots=True)
class UpdateJournal:
    path: Path
    state: str
    datum_hash: str
    checkpoint: str | None = None

    @classmethod
    def create(cls, path: Path, datum_hash: str) -> "UpdateJournal":
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"state": "prepared", "datum_hash": datum_hash, "checkpoint": None}
        with path.open("x") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        return cls(path, "prepared", datum_hash)

    @classmethod
    def load(cls, path: Path) -> "UpdateJournal":
        record = json.loads(path.read_text())
        if record["state"] not in _TRANSITIONS:
            raise ValueError("unknown transaction phase")
        return cls(path, str(record["state"]), str(record["datum_hash"]), record["checkpoint"])

    @property
    def safe_to_resume(self) -> bool:
        return self.state == "skipped" or (self.state == "checkpointed" and bool(self.checkpoint))

    def advance(self, state: str, *, checkpoint: str | None = None) -> None:
        if state not in _TRANSITIONS[self.state] or (state == "checkpointed" and not checkpoint):
            raise ValueError("invalid transaction transition")
        record = {"state": state, "datum_hash": self.datum_hash, "checkpoint": checkpoint}
        with NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
        self.state, self.checkpoint = state, checkpoint
