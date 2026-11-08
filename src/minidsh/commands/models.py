"""Resolve CLI model policy and credentials through explicit construction capabilities."""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from minidsh.adapters import (
    CredentialRedactingModel,
    MLXAdapter,
    ModelAdapterError,
    ModelProfile,
    resolve_profile,
)
from minidsh.adapters.credentials import CredentialStore
from minidsh.config import ResolvedConfig
from minidsh.domain import ModelPricing
from minidsh.ports import Model


class ProviderFactory(Protocol):
    """Construct the chosen provider without hiding credential or sampling inputs."""

    def __call__(
        self,
        profile: ModelProfile,
        *,
        environ: Mapping[str, str],
        temperature: float,
        max_attempts: int,
    ) -> Model: ...


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """The model adapter plus everything a report needs to describe it."""

    model: Model | None
    label: str
    profile: ModelProfile | None
    pricing: ModelPricing
    script: Path | None

    def describe(self) -> dict[str, Any]:
        """Return the provider facts recorded alongside results."""
        payload: dict[str, Any] = {
            "label": self.label,
            "pricing": {
                "input_usd_per_million": self.pricing.input_usd_per_million,
                "output_usd_per_million": self.pricing.output_usd_per_million,
                "cached_input_usd_per_million": self.pricing.cached_input_usd_per_million,
            },
        }
        if self.profile is not None:
            payload["profile"] = self.profile.describe()
        if isinstance(self.model, MLXAdapter):
            payload["backend"] = "mlx"
            payload["revision"] = Path(self.model.model_path).name
            payload["context_input_budget"] = self.model.input_budget
            payload["max_output_tokens"] = self.model.max_output_tokens
            payload["sampling"] = "greedy"
            payload["thinking"] = False
        return payload


def is_free(pricing: ModelPricing) -> bool:
    """Return whether both input and output rates are zero under the existing pricing policy."""
    return pricing.input_usd_per_million == 0 and pricing.output_usd_per_million == 0


def _apply_model_overrides(profile: ModelProfile, args: argparse.Namespace) -> ModelProfile:
    """Let explicit flags win over the profile's pinned endpoint, model, and key var."""
    model = getattr(args, "model", None)
    endpoint = getattr(args, "endpoint", None)
    api_key_env = getattr(args, "api_key_env", None)
    thinking = getattr(args, "thinking", None)
    if model is None and endpoint is None and api_key_env is None and thinking is None:
        return profile
    return replace(
        profile,
        model=str(model) if model else profile.model,
        endpoint=str(endpoint) if endpoint else profile.endpoint,
        api_key_env=str(api_key_env) if api_key_env else profile.api_key_env,
        thinking=profile.thinking if thinking is None else bool(thinking),
    )


def profile_for(
    args: argparse.Namespace, *, environ: Mapping[str, str] | None = None
) -> ModelProfile | None:
    """Resolve the named profile, or synthesise one from explicit endpoint flags."""
    environment = os.environ if environ is None else environ
    name = getattr(args, "model_profile", None)
    if getattr(args, "provider", None) == "scripted" and getattr(args, "provider_explicit", False):
        if name:
            raise ValueError("--provider scripted cannot be combined with --model-profile")
        return None
    if getattr(args, "provider", None) == "mlx":
        if name:
            raise ValueError("--provider mlx cannot be combined with --model-profile")
        return None
    name = name or environment.get("MINIDSH_MODEL_PROFILE")
    if name:
        return _apply_model_overrides(resolve_profile(str(name), environ=environment), args)
    if getattr(args, "provider", "scripted") != "openai-compatible":
        return None
    endpoint = getattr(args, "endpoint", None) or environment.get("MINIDSH_MODEL_ENDPOINT")
    model_name = getattr(args, "model", None) or environment.get("MINIDSH_MODEL")
    if not endpoint or not model_name:
        msg = (
            "openai-compatible requires --endpoint/MINIDSH_MODEL_ENDPOINT and "
            "--model/MINIDSH_MODEL, or a named --model-profile"
        )
        raise ModelAdapterError(msg, code="missing_endpoint")
    return ModelProfile(
        name="openai-compatible",
        endpoint=str(endpoint),
        model=str(model_name),
        api_key_env=str(
            getattr(args, "api_key_env", None)
            or environment.get("MINIDSH_API_KEY_ENV")
            or "MINIDSH_API_KEY"
        ),
        pricing=None,
        pricing_source="Explicit endpoint: declare the rate card on the command line.",
    )


def model_arguments(args: argparse.Namespace, resolved: ResolvedConfig) -> argparse.Namespace:
    """Apply resolved model settings without overriding explicitly selected offline providers."""
    copied = argparse.Namespace(**vars(args))
    if (
        getattr(args, "script", None) is not None
        or getattr(args, "provider", None) == "mlx"
        or (
            getattr(args, "provider", None) == "scripted"
            and getattr(args, "provider_explicit", False)
        )
    ):
        return copied
    for key, value in resolved.model_options.items():
        setattr(copied, key, value)
    return copied


def select_model(
    args: argparse.Namespace,
    resolved: ResolvedConfig,
    *,
    provider_factory: ProviderFactory,
    credentials_path: Callable[[], Path],
    environ: Mapping[str, str] | None = None,
) -> ModelSelection:
    """Build the model adapter and the rate card its results will be priced with."""
    args = model_arguments(args, resolved)
    environment = dict(os.environ if environ is None else environ)
    if getattr(args, "model", None):
        environment.pop("MINIDSH_MODEL", None)
    if getattr(args, "endpoint", None):
        environment.pop("MINIDSH_MODEL_ENDPOINT", None)
    profile = profile_for(args, environ=environment)
    if profile is None:
        if getattr(args, "provider", None) == "mlx":
            raw_path = getattr(args, "model", None) or environment.get("MINIDSH_MODEL")
            if not raw_path:
                raise ValueError("mlx requires --model or MINIDSH_MODEL pointing to local weights")
            model_path = Path(str(raw_path)).expanduser().resolve(strict=True)
            if not model_path.is_dir():
                raise ValueError("mlx model path must be a local directory")
            adapter = MLXAdapter(
                str(model_path),
                max_output_tokens=int(getattr(args, "mlx_max_output_tokens", 1_024)),
                input_budget=resolved.options.context_input_budget,
            )
            repository = model_path.parent.parent.name
            label = repository.removeprefix("models--").replace("--", "/")
            if not repository.startswith("models--"):
                label = model_path.name
            return ModelSelection(adapter, label, None, ModelPricing.free(), None)
        script = getattr(args, "script", None)
        return ModelSelection(
            None,
            "scripted",
            None,
            resolved.options.pricing,
            script if isinstance(script, Path) else None,
        )

    if not environment.get(profile.api_key_env, "").strip():
        store = CredentialStore(Path(args.workspace), credentials_path())
        credential = store.resolve(profile.api_key_env, environment)
        if credential.value is not None:
            environment = {**environment, profile.api_key_env: credential.value}

    # A zero rate card is indistinguishable from an undeclared one, so "declared" is
    # tracked explicitly: a profile that genuinely costs nothing per token (local
    # weights) must not be rejected as unpriced, while a paid provider must not
    # silently report $0.
    declared = not is_free(resolved.options.pricing)
    pricing = resolved.options.pricing
    if profile.pricing is not None:
        pricing = resolved.options.pricing if declared else profile.pricing
        declared = True
    if not declared and not getattr(args, "allow_unpriced", False):
        msg = (
            f"profile {profile.name!r} has no declared rate card, so cost cannot be reported. "
            "Pass --input-usd-per-million and --output-usd-per-million (and optionally "
            "--cached-input-usd-per-million) from the provider's current pricing page, "
            "or pass --allow-unpriced to run without cost accounting."
        )
        raise ValueError(msg)
    model: Model = provider_factory(
        profile,
        environ=environment,
        temperature=float(getattr(args, "temperature", 0.0)),
        max_attempts=int(getattr(args, "max_attempts", 4)),
    )
    if secret := profile.api_key(environment):
        model = CredentialRedactingModel(model, secret)
    return ModelSelection(model, profile.resolved_model(environment), profile, pricing, None)
