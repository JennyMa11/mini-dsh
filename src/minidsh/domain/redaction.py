"""Redaction of secret-looking values before they reach a trace, log, or error.

Secrets must not appear in prompts, traces, logs, test snapshots, or error
messages.  Error messages are the easiest place to leak one by accident, because
a provider's response body and echoed request fragments travel straight into
``failure_reason`` — so the rules live here, in a pure module with no
dependencies, and both the trace writer and the model adapters apply exactly the
same ones rather than each carrying their own copy.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

REDACTED = "[REDACTED]"

# Whole fields whose value is a secret by name, whatever it happens to contain.
SECRET_KEYS = frozenset(
    {"api_key", "authorization", "credential", "password", "secret", "access_token"}
)

_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|password|secret|access[_-]?token)\s*([:=])\s*([^\s,;]+)"
)
_QUOTED_FIELD = re.compile(r"""(?P<key>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')\s*:\s*""")
_SINGLE_STRING = re.compile(r"'(?:[^'\\]|\\.)*'")


def _quoted_secrets(value: str) -> str:
    replacements: list[tuple[int, int]] = []
    covered = 0
    decoder = json.JSONDecoder()
    for match in _QUOTED_FIELD.finditer(value):
        if match.start() < covered:
            continue
        literal = match.group("key")
        try:
            key = json.loads(literal) if literal.startswith('"') else ast.literal_eval(literal)
        except (ValueError, SyntaxError, UnicodeError):
            continue
        if not isinstance(key, str):
            continue
        normalized = key.lower().replace("-", "_")
        if normalized not in SECRET_KEYS and normalized not in {"apikey", "accesstoken"}:
            continue
        start = match.end()
        try:
            _, covered = decoder.raw_decode(value, start)
        except (ValueError, RecursionError):
            # Python repr strings are common in diagnostics. An unparseable
            # unquoted secret value is conservatively removed through the end.
            single = _SINGLE_STRING.match(value, start)
            covered = single.end() if single is not None else len(value)
        replacements.append((start, covered))
    for start, end in reversed(replacements):
        value = value[:start] + json.dumps(REDACTED) + value[end:]
    return value


def redact_text(value: str) -> str:
    """Redact inline credentials in a string, leaving everything else readable."""
    without_bearer = _BEARER.sub(f"Bearer {REDACTED}", _quoted_secrets(value))
    return _SECRET_ASSIGNMENT.sub(rf"\1\2{REDACTED}", without_bearer)


def redact_value(value: Any, *, key: str = "") -> Any:
    """Recursively redact a JSON-shaped value, honouring secret-bearing keys."""
    if key.lower() in SECRET_KEYS:
        return REDACTED
    if isinstance(value, dict):
        return {
            str(item_key): redact_value(item, key=str(item_key)) for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


__all__ = ["REDACTED", "SECRET_KEYS", "redact_text", "redact_value"]
