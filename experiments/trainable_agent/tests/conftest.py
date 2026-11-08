"""Never let offline contracts depend on the user's saved SDK credentials/home."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

_TEST_HOME = TemporaryDirectory(prefix="minidsh-rl-test-home-")
_HOME_PATCH = patch("pathlib.Path.home", return_value=Path(_TEST_HOME.name))
_ENV_PATCH = patch.dict(
    os.environ,
    {"PYTRIO_API_KEY": "", "PYTRIO_ENV": "", "PYTRIO_BASE_URL": "", "PYTRIO_LOG_LEVEL": ""},
)


def pytest_sessionstart(session: pytest.Session) -> None:
    del session
    _HOME_PATCH.start()
    _ENV_PATCH.start()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del session, exitstatus
    _ENV_PATCH.stop()
    _HOME_PATCH.stop()
    _TEST_HOME.cleanup()
