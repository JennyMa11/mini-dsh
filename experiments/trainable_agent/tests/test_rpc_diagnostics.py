"""SDK failures retain useful structured facts without free-form secret-bearing text."""

from types import SimpleNamespace
from typing import cast

from minidsh_rl.rpc_diagnostics import rpc_diagnostic


def test_diagnostic_excludes_message_urls_and_headers() -> None:
    error = RuntimeError("secret-value https://host/signed?token=secret-value")
    error.code = "actor.checkpoint_not_found"  # type: ignore[attr-defined]
    error.status_code = 404  # type: ignore[attr-defined]
    error.request_id = "request_123"  # type: ignore[attr-defined]
    error.details = {"authorization": "secret-value"}  # type: ignore[attr-defined]
    value = rpc_diagnostic(error)
    assert value["status_code"] == 404
    assert value["code"] == "actor.checkpoint_not_found"
    assert "secret-value" not in str(value)
    assert "https://" not in str(value)
    assert "request_123" not in str(value)


def test_untrusted_diagnostic_fields_are_bounded() -> None:
    error = cast(Exception, SimpleNamespace(code="https://signed/token", status_code=True))
    value = rpc_diagnostic(error)
    assert value["code"] is None
    assert value["status_code"] is None
