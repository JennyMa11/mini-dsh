"""Small SDK error capsules: never serialize free-form messages or transport secrets."""

import hashlib
import re

MAX_HTTP_STATUS = 599


def rpc_diagnostic(error: Exception) -> dict[str, object]:
    code = getattr(error, "code", None)
    status = getattr(error, "status_code", None)
    return {
        "error_type": type(error).__name__,
        "code": code
        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_.]{0,79}", code)
        else None,
        "status_code": status if type(status) is int and 0 <= status <= MAX_HTTP_STATUS else None,
        "retryable": getattr(error, "retryable", None) is True,
        "identifiers_sha256": {
            name: hashlib.sha256(value.encode()).hexdigest()
            for name in ("request_id", "event_cuid", "actor_cuid")
            if isinstance(value := getattr(error, name, None), str) and value
        },
        "automatic_retry": False,
    }
