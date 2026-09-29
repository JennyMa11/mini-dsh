"""Replaceable model and persistence adapters."""

from minidsh.adapters.mlx import MLXAdapter
from minidsh.adapters.models import (
    CredentialRedactingModel,
    ModelAdapterError,
    OpenAICompatibleModel,
    ScriptedModel,
)
from minidsh.adapters.profiles import (
    BUILTIN_PROFILES,
    ModelProfile,
    build_provider_model,
    resolve_profile,
)
from minidsh.adapters.sessions import JsonSessionStore, SessionStoreError
from minidsh.adapters.trace import JsonlTraceWriter

__all__ = [
    "BUILTIN_PROFILES",
    "CredentialRedactingModel",
    "JsonSessionStore",
    "JsonlTraceWriter",
    "MLXAdapter",
    "ModelAdapterError",
    "ModelProfile",
    "OpenAICompatibleModel",
    "ScriptedModel",
    "SessionStoreError",
    "build_provider_model",
    "resolve_profile",
]
