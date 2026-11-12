"""Pure observability configuration; no transport or vendor SDK lives here."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_PHOENIX_ENDPOINT = "http://localhost:6006"
DEFAULT_PHOENIX_PROJECT = "mini-dsh"
MAX_PHOENIX_PROJECT_LENGTH = 128

type AttributeValue = str | bool | int | float


@dataclass(frozen=True, slots=True)
class PhoenixConfig:
    """Where and how runtime facts are exported to an OTLP-compatible Phoenix.

    ``endpoint`` accepts either the Phoenix base URL or the full OTLP HTTP path.
    Credentials in the URL are rejected so a trace endpoint cannot become a
    secret-bearing value in a config dump.
    """

    endpoint: str = DEFAULT_PHOENIX_ENDPOINT
    project_name: str = DEFAULT_PHOENIX_PROJECT
    capture_content: bool = True

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("phoenix endpoint must be an absolute http(s) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("phoenix endpoint must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("phoenix endpoint must not contain a query or fragment")
        project = self.project_name.strip()
        if not project or len(project) > MAX_PHOENIX_PROJECT_LENGTH:
            raise ValueError(
                f"phoenix project name must be 1-{MAX_PHOENIX_PROJECT_LENGTH} characters"
            )
        object.__setattr__(self, "project_name", project)

    @property
    def traces_endpoint(self) -> str:
        """Return the OTLP HTTP traces URL implied by the configured endpoint."""
        base = self.endpoint.rstrip("/")
        if urlsplit(base).path in {"", "/"}:
            return f"{base}/v1/traces"
        return base


__all__ = [
    "DEFAULT_PHOENIX_ENDPOINT",
    "DEFAULT_PHOENIX_PROJECT",
    "MAX_PHOENIX_PROJECT_LENGTH",
    "AttributeValue",
    "PhoenixConfig",
]
