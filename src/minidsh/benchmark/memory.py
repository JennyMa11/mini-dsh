"""Optional host memory observations for local inference measurements."""

from __future__ import annotations

import asyncio
import ctypes
import os
import platform
import re
import subprocess
from dataclasses import dataclass


def process_footprint_bytes() -> int | None:
    """Read this process's phys_footprint via macOS proc_pid_rusage v2."""
    if platform.system() != "Darwin":
        return None
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        call = libproc.proc_pid_rusage
        call.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        call.restype = ctypes.c_int
        buffer = (ctypes.c_ubyte * 256)()
        if call(os.getpid(), 2, ctypes.byref(buffer)) != 0:
            return None
        # rusage_info_v2: uuid[16], then seven uint64 values before footprint.
        return int.from_bytes(bytes(buffer[72:80]), "little")
    except (AttributeError, OSError):
        return None


def system_free_percent() -> float | None:
    """Collect macOS memory pressure as context, not as model memory."""
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/memory_pressure", "-Q"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"System-wide memory free percentage:\s*([\d.]+)%", result.stdout)
    return float(match.group(1)) if match else None


@dataclass(slots=True)
class MemorySample:
    """Peak process footprint and surrounding system pressure."""

    peak_bytes: int | None = None
    before_percent: float | None = None
    after_percent: float | None = None

    async def watch(self, stop: asyncio.Event, interval_seconds: float = 0.1) -> None:
        """Sample until stop is set, including the final boundary sample."""
        self.before_percent = await asyncio.to_thread(system_free_percent)
        while not stop.is_set():
            current = process_footprint_bytes()
            if current is not None:
                self.peak_bytes = max(self.peak_bytes or 0, current)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue
        current = process_footprint_bytes()
        if current is not None:
            self.peak_bytes = max(self.peak_bytes or 0, current)
        self.after_percent = await asyncio.to_thread(system_free_percent)
