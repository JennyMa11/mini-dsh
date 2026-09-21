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
        }


_DEEPSEEK_PRICING_NOTE = (
    "DeepSeek bills in CNY on a peak/off-peak schedule and revises rates frequently. "
    "Read the current rates from the official pricing page and pass them explicitly "
    "(--input-usd-per-million / --output-usd-per-million / --cached-input-usd-per-million) "
    "before quoting any cost number."
)

BUILTIN_PROFILES: dict[str, ModelProfile] = {
    "deepseek": ModelProfile(
        name="deepseek",
        endpoint="https://api.deepseek.com/v1/chat/completions",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
        context_window=128_000,
        pricing=None,
        pricing_source=_DEEPSEEK_PRICING_NOTE,
    ),
    "deepseek-reasoner": ModelProfile(
        name="deepseek-reasoner",
        endpoint="https://api.deepseek.com/v1/chat/completions",
        model="deepseek-reasoner",
        api_key_env="DEEPSEEK_API_KEY",
        context_window=128_000,
        pricing=None,
        pricing_source=_DEEPSEEK_PRICING_NOTE,
        notes="Reasoning models reject some sampling parameters and are much slower per step.",
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
    )


__all__ = [
    "BUILTIN_PROFILES",
    "ModelProfile",
    "ScriptedModel",
    "build_provider_model",
    "resolve_profile",
]
