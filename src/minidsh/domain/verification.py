"""Application-owned visible criterion; no execution or workspace access."""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_VERIFICATION_SECONDS = 300


@dataclass(frozen=True, slots=True)
class VerificationConfig:
    argv: tuple[str, ...]
    criterion_version: str
    cwd: str = "."
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.argv, tuple)
            or not self.argv
            or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in self.argv)
        ):
            raise ValueError("verification argv requires nonempty fixed arguments")
        path = PurePosixPath(self.cwd)
        if (
            not self.cwd
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in self.cwd
            or "\x00" in self.cwd
            or path.as_posix() != self.cwd
        ):
            raise ValueError("verification cwd must be canonical and workspace-relative")
        if (
            not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= MAX_VERIFICATION_SECONDS
        ):
            raise ValueError("verification timeout must be in (0, 300]")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", self.criterion_version) is None:
            raise ValueError("verification requires a bounded criterion version")

    @property
    def sha256(self) -> str:
        value = {
            "argv": self.argv,
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "criterion_version": self.criterion_version,
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
