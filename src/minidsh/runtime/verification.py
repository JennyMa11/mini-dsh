"""Bounded visible verification using only a phase-owned controlled caller."""

from collections.abc import Callable

from minidsh.domain import Event, RunResult, ToolStatus, redact_text
from minidsh.domain.extensions import (
    MAX_FINISH_FEEDBACK,
    ExtensionBudgetExhaustedError,
    FinishAction,
    FinishDecision,
    FinishProposal,
)
from minidsh.domain.verification import VerificationConfig
from minidsh.ports import EventSink
from minidsh.runtime.hooks import DecisionHook

MAX_FEEDBACK_REPAIRS = 2
MAX_COMPLETION_CHECKS = MAX_FEEDBACK_REPAIRS + 1


class VerificationExtension:
    def __init__(
        self,
        config: VerificationConfig,
        events: EventSink,
        is_final: Callable[[DecisionHook], bool],
    ) -> None:
        self._config = config
        self._events = events
        self._is_final = is_final
        self._checks: dict[str, int] = {}

    async def before_finish(  # noqa: PLR0912 - explicit outcome and bounded repair policy
        self, proposal: FinishProposal
    ) -> FinishDecision:
        if proposal.caller is None:
            return FinishDecision(FinishAction.FAIL, "verification_unavailable")
        if not self._is_final(self.before_finish):
            return FinishDecision(FinishAction.FAIL, "verification_order_invalid")
        run_id = proposal.view.run_id
        attempt = self._checks.get(run_id, 0) + 1
        if attempt > MAX_COMPLETION_CHECKS:
            return FinishDecision(FinishAction.FAIL, "verification_failed: repair limit reached")
        self._checks[run_id] = attempt
        observed = await proposal.caller.invoke("test", {})
        result = observed.result
        verdict, reason, feedback = "error", "verification_tool_error", ""
        if observed.status is ToolStatus.TIMED_OUT or (
            isinstance(result, dict) and result.get("timed_out") is True
        ):
            reason = "verification_timeout"
        elif observed.status is not ToolStatus.SUCCEEDED:
            reason = "verification_" + (
                observed.error.code if observed.error else observed.status.value
            )
        elif observed.effective_call is None:
            reason = "verification_call_untracked"
        elif observed.effective_call.name != "test" or observed.effective_call.arguments:
            reason = "verification_call_rewritten"
        elif (
            not isinstance(result, dict)
            or type(result.get("exit_code")) is not int
            or type(result.get("timed_out")) is not bool
        ):
            reason = "verification_result_invalid"
        elif result.get("timed_out") is True:
            reason = "verification_timeout"
        elif result["exit_code"] == 0:
            verdict, reason = "passed", ""
        elif result["exit_code"] == 1:
            verdict, reason = "failed", "verification_failed"
            feedback = "\n".join(str(result.get(key, "")) for key in ("stdout", "stderr"))
            if observed.truncated:
                feedback += "\n" + str(result.get("diagnostics", ""))
        else:
            reason = "verification_command_error"
        await self._events.publish(
            Event(
                "runtime_verification_completed",
                run_id,
                proposal.view.step_id,
                {
                    "runtime_verification": verdict,
                    "attempt": attempt,
                    "call_id": observed.call_id,
                    "criterion_version": self._config.criterion_version,
                    "command_sha256": self._config.sha256,
                    "reason": reason,
                },
            )
        )
        if verdict == "passed":
            return FinishDecision()
        remaining = proposal.caller.snapshot().remaining
        if verdict == "failed" and remaining is not None and remaining.steps <= 0:
            raise ExtensionBudgetExhaustedError("step budget exhausted")
        if verdict == "error" or attempt == MAX_COMPLETION_CHECKS:
            return FinishDecision(FinishAction.FAIL, reason)
        header = (
            f"verification_failed: visible criterion {self._config.criterion_version}; "
            f"repair {attempt}/2. Fix the implementation before finishing again.\n"
        )
        detail = redact_text(feedback)[-(MAX_FINISH_FEEDBACK - len(header)) :]
        return FinishDecision(FinishAction.CONTINUE, header + detail)

    async def on_run_end(self, result: RunResult) -> None:
        self._checks.pop(result.run_id, None)

    def clear(self) -> None:
        self._checks.clear()
