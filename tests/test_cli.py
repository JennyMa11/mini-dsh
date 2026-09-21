"""Smoke tests for the command-line interface."""

from __future__ import annotations

import json

import pytest

from minidsh.cli import main


def test_doctor_reports_healthy_environment(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["doctor"])

    assert exit_info.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["python_ok"] is True


def test_version_is_available(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("mini-dsh ")
