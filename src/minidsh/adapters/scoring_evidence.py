"""Decode independent verification sidecars shared by Memory and offline scoring."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

type Checks = dict[Annotated[str, Field(min_length=1)], StrictBool | None]


class ScoringBudget(BaseModel):
    """Only explicitly supplied, positive limits are scored."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    max_steps: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_tool_calls: int | None = Field(default=None, gt=0)
    max_duration_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class ScoringEvidence(BaseModel):
    """Independent verifier results or reviewed annotations tied to one run.

    All checks use True for the expected behavior: unsafe actions blocked, safe
    actions allowed, secrets absent, and policies obeyed. None means unchecked.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    case_id: str | None = None
    task_success: StrictBool | None = None
    user_accepted: StrictBool | None = None
    human_escalated: StrictBool | None = None
    budget: ScoringBudget | None = None
    goal_checks: Checks = Field(default_factory=dict)
    constraint_checks: Checks = Field(default_factory=dict)
    tool_selection_checks: Checks = Field(default_factory=dict)
    recovery_checks: Checks = Field(default_factory=dict)
    unsafe_action_checks: Checks = Field(default_factory=dict)
    safe_action_checks: Checks = Field(default_factory=dict)
    secret_checks: Checks = Field(default_factory=dict)
    policy_checks: Checks = Field(default_factory=dict)

    @classmethod
    def from_file(cls, path: Path) -> ScoringEvidence:
        """Read a sidecar without exposing its contents in validation errors."""
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            raise ValueError("cannot read valid scoring evidence") from exc
