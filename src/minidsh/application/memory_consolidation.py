"""Budgeted preparation through a separate, model-inaccessible tool executor."""

import json
import time
from collections.abc import Callable

from minidsh.domain import ContextView, Message, Role, ToolCall, ToolStatus, estimate_tokens
from minidsh.domain.extensions import ExtensionBudgetExhaustedError
from minidsh.domain.harness import PreparedContext
from minidsh.ports import ToolExecutor


class ConsolidationContext:
    def __init__(self, inputs: ToolExecutor) -> None:
        self._inputs = inputs

    def build(self, history: tuple[Message, ...], task: str) -> ContextView:
        del history, task
        raise ValueError("consolidation_requires_preparation")

    async def prepare(  # noqa: PLR0913, PLR0917 - existing prepared-context port
        self,
        history: tuple[Message, ...],
        task: str,
        run_id: str,
        step_id: int,
        max_calls: int,
        account: Callable[[int, float], None] | None = None,
    ) -> PreparedContext:
        del task
        if max_calls <= 0:
            raise ExtensionBudgetExhaustedError("consolidation input tool budget exhausted")
        started = time.monotonic()
        if account:
            account(1, 0.0)
        try:
            observed = await self._inputs.execute(
                ToolCall(f"consolidation-input-{step_id}", "load_consolidation_inputs", {}),
                run_id,
                step_id,
            )
        finally:
            duration = (time.monotonic() - started) * 1000
            if account:
                account(0, duration)
        if observed.status is not ToolStatus.SUCCEEDED or observed.truncated:
            raise ValueError("memory_consolidation_inputs_unavailable")
        messages = (
            *history,
            Message(
                Role.USER, json.dumps(observed.result, ensure_ascii=False), name="project_memory"
            ),
        )
        return PreparedContext(
            ContextView(messages, "memory_consolidation", "1.0", estimate_tokens(messages)),
            1,
            duration,
        )
