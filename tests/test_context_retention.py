"""All trimming views preserve current goals and explicitly tagged facts."""

import pytest

from minidsh.context import SlidingWindowContext, SummaryCompactionContext
from minidsh.domain import Message, Role


@pytest.mark.parametrize("strategy", [SlidingWindowContext(2), SummaryCompactionContext(2)])
def test_old_critical_state_and_current_task_survive(
    strategy: SlidingWindowContext | SummaryCompactionContext,
) -> None:
    history = (
        Message(Role.SYSTEM, "rules"),
        Message(Role.USER, "initial"),
        Message(Role.SYSTEM, "keep this decision", name="critical_decision"),
        Message(Role.SYSTEM, "pending error", name="open_error"),
        Message(Role.USER, "latest goal"),
        *(Message(Role.ASSISTANT, f"step {index}") for index in range(20)),
    )
    view = strategy.build(history, "latest goal")
    contents = {message.content for message in view.messages}
    assert {"rules", "initial", "keep this decision", "pending error", "latest goal"} <= contents
    assert history[2].content == "keep this decision"
