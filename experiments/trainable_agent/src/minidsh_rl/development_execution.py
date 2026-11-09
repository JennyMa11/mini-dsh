"""One complete sampling-only admission queue; no optimizer or training interface."""

import json
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from minidsh.infrastructure.locks import FileLease
from minidsh_rl.curve_artifacts import tree_hashes, verify_hashes
from minidsh_rl.curve_schedule import CurveRow, readiness_gate
from minidsh_rl.development_budget import StudyBudget
from minidsh_rl.development_cases import development_cases
from minidsh_rl.development_schedule import DevelopmentPlan, make_development_plan
from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.learning_gates import _sources
from minidsh_rl.learning_rollout import LearningRollout, PolicyBinding
from minidsh_rl.learning_schedule import RUN_COST, atomic_record, digest

Sampler = Callable[[LearningCase, CurveRow, Path], Awaitable[LearningRollout]]
Feedback = Callable[[LearningRollout, Path], Awaitable[bool]]


class DevelopmentState(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1
    phase: Literal["running", "accepted", "rejected"] = "running"
    plan_sha256: str
    policy: PolicyBinding
    pending: str | None = None
    completed: list[str] = Field(default_factory=list)
    rewards: dict[str, list[bool]] = Field(default_factory=dict)
    families: dict[str, str] = Field(default_factory=dict)
    recovered_families: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    facts: list[dict[str, str]] = Field(default_factory=list)


async def execute_development(  # noqa: PLR0913, PLR0917, PLR0915, PLR0912 - explicit sampling-only state machine
    root: Path,
    plan: DevelopmentPlan,
    budget: StudyBudget,
    policy: PolicyBinding,
    sampler: Sampler,
    feedback: Feedback,
) -> DevelopmentState:
    """Unknown/interrupted rows are never automatically replayed or replaced."""
    lease = FileLease(root / "controller.lock")
    try:
        plan_path = root / "plan.json"
        expected = make_development_plan()
        if (
            DevelopmentPlan.model_validate_json(plan_path.read_bytes()) != plan
            or plan.rows != expected.rows
            or plan.case_sha256 != expected.case_sha256
        ):
            raise ValueError("frozen development queue changed")
        if (plan.kind == "provider") != (policy.kind == "provider"):
            raise ValueError("runtime provenance mismatch")
        path = root / "state.json"
        if path.exists():
            state = DevelopmentState.model_validate_json(path.read_bytes())
            if state.plan_sha256 != digest(plan_path) or state.policy != policy:
                raise ValueError("state/plan/policy changed")
            verify_hashes(root, state.artifacts)
            if state.phase != "running":
                return state
            raise ValueError("interrupted admission; audit pending outcome, never replay")
        if budget.available < plan.required_cny:
            raise ValueError("complete development allocation unavailable")
        state = DevelopmentState(plan_sha256=digest(plan_path), policy=policy)

        def save(event: str) -> None:
            state.facts.append({"name": event, "occurred_at": datetime.now(UTC).isoformat()})
            atomic_record(path, state)

        save("admission_started")
        cases = {case.task.case_id: case for case in development_cases()}
        for index, row in enumerate(plan.rows):
            if plan.kind == "provider" and plan.sources != _sources():
                raise ValueError("source changed during development")
            if digest(plan_path) != state.plan_sha256:
                raise ValueError("plan changed during development")
            if budget.available < (len(plan.rows) - index) * RUN_COST:
                raise ValueError("complete remaining development allocation unavailable")
            state.pending = row.run_id
            save("rollout_started")
            folder = root / "rollouts" / row.run_id
            value = await sampler(cases[row.case_id], row, folder)
            if (
                value.case != cases[row.case_id]
                or value.run_id != row.run_id
                or value.seed != row.seed
                or value.policy != policy
                or value.budget_violations
                or value.status == "cancelled"
            ):
                raise ValueError("trajectory identity/budget/cancellation mismatch")
            state.completed.append(row.run_id)
            state.rewards.setdefault(row.group, []).append(value.success)
            state.families[row.group] = value.case.family_group
            state.artifacts.update(tree_hashes(root, folder))
            state.pending = None
            save("rollout_completed")
            if value.success and await feedback(value, root / "feedback" / row.run_id):
                state.recovered_families = sorted(
                    set(state.recovered_families) | {value.case.family_group}
                )
            state.artifacts.update(tree_hashes(root, root / "feedback"))
            save("feedback_checked")
            sys.stdout.write(
                json.dumps(
                    {
                        "phase": "development",
                        "completed": index + 1,
                        "declared": len(plan.rows),
                        "success": value.success,
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
        accepted = readiness_gate(
            list(state.rewards.values()),
            list(state.families.values()),
            set(state.recovered_families),
        )
        state.phase = "accepted" if accepted else "rejected"
        save("development_finished")
        return state
    finally:
        lease.close()
