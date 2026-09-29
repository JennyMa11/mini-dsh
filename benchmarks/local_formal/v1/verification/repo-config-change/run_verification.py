"""Run this task's held-out checks.

Supplied by the benchmark overlay, so it is absent while the agent works and cannot be
edited by it.  Discovery is pinned to ``verification_tests/`` and the number of
collected checks is asserted: a workspace whose suite was deleted, or whose imports no
longer resolve, fails here instead of passing vacuously.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    minimum = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    checks = HERE / "verification_tests"
    # Checked before discovery because `TestLoader.discover` raises on a missing start
    # directory: a traceback would exit non-zero too, but it would read as a harness
    # crash rather than as the task failing to provide its checks.
    if not checks.is_dir():
        print(f"the held-out check directory is missing: {checks}", file=sys.stderr)
        return 2
    suite = unittest.TestLoader().discover(str(checks), top_level_dir=str(HERE))
    collected = suite.countTestCases()
    print(f"collected {collected} held-out checks (minimum {minimum})")
    if collected < minimum:
        print("fewer checks were collected than this task declares", file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
