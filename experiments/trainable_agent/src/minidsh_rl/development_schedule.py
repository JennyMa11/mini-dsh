"""Sampling-only developmental admission; no train/test/checkpoint selection rows."""

from decimal import Decimal
from typing import Literal

from pydantic import Field

from minidsh_rl.curve_schedule import CurveRow
from minidsh_rl.development_cases import development_cases
from minidsh_rl.learning_rollout import _case_digest
from minidsh_rl.learning_schedule import RUN_COST, FrozenRecord

DECLARED = 24
CAP = Decimal("3")
SETUP = Decimal(".06")


class DevelopmentPlan(FrozenRecord):
    schema_version: Literal[1] = 1
    dataset_version: Literal["curve_development_v3"] = "curve_development_v3"
    kind: Literal["provider", "contract_fixture"] = "provider"
    rows: tuple[CurveRow, ...]
    case_sha256: dict[str, str]
    required_cny: Decimal = DECLARED * RUN_COST + SETUP
    cap_cny: Literal[3] = 3
    sources: dict[str, str] = Field(default_factory=dict)
    inputs: dict[str, str] = Field(default_factory=dict)
    finance_directory: str = ""
    start_committed: Decimal | None = None


def make_development_plan() -> DevelopmentPlan:
    cases = development_cases()
    rows = tuple(
        CurveRow(
            run_id=f"dev3_{i:02}_{repeat}",
            phase="dev",
            case_id=case.task.case_id,
            seed=926000 + i * 1000 + repeat * 20,
            group=f"dev_{i:02}",
            checkpoint=0,
        )
        for i, case in enumerate(cases)
        for repeat in range(4)
    )
    return DevelopmentPlan(rows=rows, case_sha256={c.task.case_id: _case_digest(c) for c in cases})
