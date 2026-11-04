"""Frozen cohort and unknown-usage admission contracts."""

import json

import pytest

from minidsh.benchmark.mini_dsh import CASE_IDS, PROTOCOL, CohortBudget, validate_protocol
from minidsh.domain import TokenUsage


def test_unknown_usage_stops_following_calls() -> None:
    budget = CohortBudget()
    budget.begin_case()
    budget.record(None)
    assert budget.unknown_calls == 1
    assert not budget.admits(1)


def test_failed_and_over_budget_usage_remains_counted() -> None:
    budget = CohortBudget()
    budget.begin_case()
    budget.record(TokenUsage(8100, 200))
    assert budget.total == 8300
    assert not budget.admits(1)
    budget.begin_case()
    assert budget.total == 8300
    assert budget.admits(1)


def test_declared_cohort_is_fixed() -> None:
    assert len(CASE_IDS) * 2 == 12
    with pytest.raises(ValueError, match="positive"):
        CohortBudget(total_limit=-1)


@pytest.mark.parametrize(
    "field", ["case_tokens", "steps", "tool_calls", "seconds", "image", "isolation"]
)
def test_changed_execution_protocol_is_rejected(field: str) -> None:
    protocol = json.loads(PROTOCOL.read_text())
    validate_protocol(protocol)
    protocol[field] = None
    with pytest.raises(ValueError, match="protocol"):
        validate_protocol(protocol)
