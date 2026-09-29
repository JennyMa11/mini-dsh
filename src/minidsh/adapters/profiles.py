"""Named provider profiles for real-model evaluation runs.

A profile pins *where* a model is reached, *which* model name is requested, and
*which* environment variable holds the credential. It deliberately does **not**
pin pricing: published rates change without notice, and a stale rate card silently
corrupts every cost number in a report. Real providers therefore ship with
``pricing=None`` and a run refuses to account cost until the caller declares an
explicit rate card, so a published cost can always be traced back to a stated
assumption.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from minidsh.adapters.models import ModelAdapterError, OpenAICompatibleModel, ScriptedModel
from minidsh.domain import ModelPricing
from minidsh.ports import Model


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """Endpoint, model name, credential source, and rate card for one provider."""

    name: str
    endpoint: str
    model: str
    api_key_env: str = "MINIDSH_API_KEY"
    context_window: int = 128_000
    requires_api_key: bool = True
    supports_tool_calling: bool = True
    # Chain-of-thought as a request-level toggle.  Declared separately from
    # ``thinking`` so that a provider which does not understand the parameter
    # never receives it, while a provider that does can still default it off.
    supports_thinking: bool = False
    thinking: bool = False
    pricing: ModelPricing | None = None
    pricing_source: str = ""
    pricing_as_of: str = ""
    notes: str = ""

    def resolved_endpoint(self, environ: Mapping[str, str]) -> str:
        """Return the endpoint, allowing an environment override."""
        return environ.get("MINIDSH_MODEL_ENDPOINT", self.endpoint)

    def resolved_model(self, environ: Mapping[str, str]) -> str:
        """Return the model name, allowing an environment override."""
        return environ.get("MINIDSH_MODEL", self.model)

    def api_key(self, environ: Mapping[str, str]) -> str:
        """Return the credential from its declared variable, or raise a safe error."""
        value = environ.get(self.api_key_env, "")
        if not value and self.requires_api_key:
            msg = (
                f"profile {self.name!r} requires an API key in {self.api_key_env}; "
                "export it before running a real-model experiment"
            )
            raise ModelAdapterError(msg, code="missing_credentials")
        return value

    def describe(self) -> dict[str, object]:
        """Return the provider facts recorded in a benchmark report."""
        return {
            "profile": self.name,
            "model": self.model,
            "endpoint": self.endpoint,
            "context_window": self.context_window,
            "pricing_declared": self.pricing is not None,
            "pricing_source": self.pricing_source,
            "pricing_as_of": self.pricing_as_of,
            "thinking": self.thinking if self.supports_thinking else None,
            "temperature_effective": not (self.supports_thinking and self.thinking),
        }


_DEEPSEEK_PRICING_NOTE = (
    "DeepSeek publishes USD rates on a peak/off-peak schedule: off-peak is half of "
    "peak, and peak hours are 01:00-04:00 and 06:00-10:00 UTC Monday-Friday, "
    "excluding Chinese public holidays. A single rate card therefore cannot "
    "describe a run that crosses a tariff boundary, so the rates are deliberately "
    "not pinned here. As of 2026-09-21 the official page lists deepseek-flash at "
    "$0.003 cache-hit input / $0.15 cache-miss input / $0.6 output per 1M tokens "
    "off-peak (double at peak), and deepseek-v4-pro at $0.022 / $0.66 / $1.98. "
    "Read https://api-docs.deepseek.com/quick_start/pricing and pass the rates "
    "explicitly (--input-usd-per-million / --output-usd-per-million / "
    "--cached-input-usd-per-million) before quoting any cost number."
)

_DEEPSEEK_THINKING_NOTE = (
    "Thinking is ON by default upstream and the harness turns it off: in thinking "
    "mode the API ignores temperature, so a run cannot be pinned to temperature=0 "
    "while it is on. Turn it back on with --thinking, which also makes the harness "
    "echo reasoning_content back on every tool-carrying request (the API rejects "
    "the request with a 400 otherwise)."
)

BUILTIN_PROFILES: dict[str, ModelProfile] = {
    "deepseek": ModelProfile(
        name="deepseek",
        endpoint="https://api.deepseek.com/chat/completions",
        model="deepseek-flash",
        api_key_env="DEEPSEEK_API_KEY",
        context_window=1_000_000,
        supports_thinking=True,
        thinking=False,
        pricing=None,
        pricing_source=_DEEPSEEK_PRICING_NOTE,
        notes=_DEEPSEEK_THINKING_NOTE,
    ),
    "deepseek-v4-pro": ModelProfile(
        name="deepseek-v4-pro",
        endpoint="https://api.deepseek.com/chat/completions",
        model="deepseek-v4-pro",
        api_key_env="DEEPSEEK_API_KEY",
        context_window=1_000_000,
        supports_thinking=True,
        thinking=False,
        pricing=None,
        pricing_source=_DEEPSEEK_PRICING_NOTE,
        notes=_DEEPSEEK_THINKING_NOTE + " Roughly 3x the Flash price.",
    ),
    "openai": ModelProfile(
        name="openai",
        endpoint="https://api.openai.com/v1/chat/completions",
        model="",
        api_key_env="OPENAI_API_KEY",
        context_window=128_000,
        pricing=None,
        pricing_source=(
            "Read the current rates from the OpenAI pricing page and pass them explicitly."
        ),
        notes="Set MINIDSH_MODEL to the exact model snapshot you want to pin.",
    ),
    "ollama": ModelProfile(
        name="ollama",
        endpoint="http://127.0.0.1:11434/v1/chat/completions",
        model="",
        api_key_env="MINIDSH_API_KEY",
        context_window=32_768,
        requires_api_key=False,
        pricing=ModelPricing.free(),
        pricing_source=(
            "Local weights: no per-token provider charge. GPU time is not accounted here."
        ),
        notes="Local endpoint; set MINIDSH_MODEL to the pulled tag, for example qwen2.5-coder:7b.",
    ),
    "llama_cpp": ModelProfile(
        name="llama_cpp",
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="",
        api_key_env="MINIDSH_API_KEY",
        context_window=8_192,
        requires_api_key=False,
        pricing=ModelPricing.free(),
        pricing_source="Local weights: no per-token provider charge; machine time is separate.",
        notes="Start llama.cpp's OpenAI-compatible server and set MINIDSH_MODEL.",
    ),
}


def resolve_profile(name: str, *, environ: Mapping[str, str] | None = None) -> ModelProfile:
    """Return a built-in profile, optionally overridden by environment variables."""
    try:
        profile = BUILTIN_PROFILES[name]
    except KeyError as exc:
        msg = f"unknown model profile: {name}; choose from {sorted(BUILTIN_PROFILES)}"
        raise ModelAdapterError(msg, code="unknown_profile") from exc
    if not environ:
        return profile
    return ModelProfile(
        name=profile.name,
        endpoint=profile.resolved_endpoint(environ),
        model=profile.resolved_model(environ),
        api_key_env=environ.get("MINIDSH_API_KEY_ENV", profile.api_key_env),
        context_window=profile.context_window,
        requires_api_key=profile.requires_api_key,
        supports_tool_calling=profile.supports_tool_calling,
        supports_thinking=profile.supports_thinking,
        thinking=profile.thinking,
        pricing=profile.pricing,
        pricing_source=profile.pricing_source,
        pricing_as_of=profile.pricing_as_of,
        notes=profile.notes,
    )


def build_provider_model(
    profile: ModelProfile,
    *,
    environ: Mapping[str, str],
    temperature: float = 0.0,
    timeout_seconds: float = 120.0,
    max_attempts: int = 4,
) -> Model:
    """Build the OpenAI-compatible adapter described by one profile."""
    model_name = profile.resolved_model(environ)
    if not model_name:
        msg = (
            f"profile {profile.name!r} has no pinned model; pass --model or set MINIDSH_MODEL "
            "to the exact snapshot under test"
        )
        raise ModelAdapterError(msg, code="missing_model")
    return OpenAICompatibleModel(
        endpoint=profile.resolved_endpoint(environ),
        model=model_name,
        api_key=profile.api_key(environ),
        timeout_seconds=timeout_seconds,
        temperature=temperature,
        max_attempts=max_attempts,
        # None means "this provider has no such parameter", which keeps the field
        # out of the request body entirely rather than sending it as false.
        thinking=profile.thinking if profile.supports_thinking else None,
    )


__all__ = [
    "BUILTIN_PROFILES",
    "ModelProfile",
    "ScriptedModel",
    "build_provider_model",
    "resolve_profile",
]
