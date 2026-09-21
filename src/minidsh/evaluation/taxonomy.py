"""Attribute a failed run to the layer that actually caused it.

The whole point of this module is to keep two questions separate:

* **Harness correctness** — did the runtime do what it promised (validate, authorize,
  execute, bound, verify)? A ``permission_denied`` or ``invalid_tool_call`` failure is
  *evidence the harness worked*: it stopped the agent from doing something it was not
  allowed to do.
* **Agent capability** — given a correct harness, did the model produce a working
  change? A ``model_reasoning_failure`` or ``verification_failed`` failure is the
  interesting one for a real-model experiment.

A single blended "success rate" hides that distinction, which is exactly why the
deterministic suites report 100% and why that number cannot be used as a capability
claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from minidsh.domain import FailureCategory, JsonValue
from minidsh.evaluation.tracefile import TraceRun, as_int, as_str

_LOOP_STUCK_REPEATS = 3

# Which layer owns the outcome. ``harness`` means the runtime behaved exactly as
# specified and the agent misused it, so the runtime is *exonerated* by that failure.
_LAYER_BY_CATEGORY: dict[FailureCategory, str] = {
    FailureCategory.NONE: "success",
    FailureCategory.PERMISSION_DENIED: "harness",
    FailureCategory.INVALID_TOOL_CALL: "harness",
    FailureCategory.TOOL_ERROR: "harness",
    FailureCategory.BUDGET_EXHAUSTED: "harness",
    FailureCategory.MODEL_REASONING_FAILURE: "agent",
    FailureCategory.CONTEXT_MISS: "agent",
    FailureCategory.VERIFICATION_FAILED: "agent",
    FailureCategory.LOOP_STUCK: "agent",
    FailureCategory.PROVIDER_ERROR: "environment",
}


@dataclass(frozen=True, slots=True)
class FailureDiagnosis:
    """One classified failure with the evidence that justified it."""

    category: FailureCategory
    reason: str
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def layer(self) -> str:
        """Return ``success``, ``harness``, ``agent``, or ``environment``."""
        return _LAYER_BY_CATEGORY[self.category]

    @property
    def is_harness_layer(self) -> bool:
        """Return whether the runtime, rather than the model, is the story here."""
        return self.layer == "harness"

    def to_json(self) -> dict[str, JsonValue]:
        """Return a stable serialization for reports."""
        return {
            "category": self.category.value,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "layer": self.layer,
        }


def classify_failure(  # noqa: PLR0911 - a decision table reads better than a nested if-tree
    *,
    status: str,
    verified: bool | None,
    trace: TraceRun | None = None,
    verifier_exit_code: int | None = None,
    failure_reason: str | None = None,
) -> FailureDiagnosis:
    """Return the most specific category the available evidence supports.

    The order of checks is deliberate: unambiguous harness-level facts are tested
    before inference about the model, so a diagnosis never blames the model for
    something the runtime already recorded as a hard, structured fact.
    """
    if status == "finished" and verified is True:
        return FailureDiagnosis(FailureCategory.NONE, "verified success")
    if status == "cancelled":
        return FailureDiagnosis(FailureCategory.NONE, "run cancelled by the caller")

    if status == "budget_exhausted":
        return FailureDiagnosis(
            FailureCategory.BUDGET_EXHAUSTED,
            failure_reason or "a run budget was exhausted",
            ("run_finished:budget_exhausted",),
        )

    provider = _provider_failure(trace)
    if provider is not None:
        return provider

    if trace is not None:
        stuck = _loop_stuck(trace)
        if stuck is not None:
            return stuck

    denied = _denials(trace)
    if denied:
        return FailureDiagnosis(
            FailureCategory.PERMISSION_DENIED,
            "the agent needed a capability the run did not grant",
            tuple(denied),
        )

    invalid = _invalid_calls(trace)
    if invalid:
        return FailureDiagnosis(
            FailureCategory.INVALID_TOOL_CALL,
            "the model issued a call the runtime could not accept",
            tuple(invalid),
        )

    tool_errors = _unresolved_tool_errors(trace)
    if tool_errors:
        return FailureDiagnosis(
            FailureCategory.TOOL_ERROR,
            "a tool the agent depended on failed and was not recovered",
            tuple(tool_errors),
        )

    if status == "failed":
        return FailureDiagnosis(
            FailureCategory.MODEL_REASONING_FAILURE,
            failure_reason or "the run raised an unrecoverable error",
            ("run_finished:failed",),
        )

    if verified is False:
        detail = _verification_detail(trace, verifier_exit_code)
        return FailureDiagnosis(*detail)

    return FailureDiagnosis(
        FailureCategory.MODEL_REASONING_FAILURE,
        failure_reason or "the run ended without a verified success",
        (f"status:{status}",),
    )


def _provider_failure(trace: TraceRun | None) -> FailureDiagnosis | None:
    """Detect an unrecovered model-provider failure recorded by the loop."""
    if trace is None:
        return None
    reason = trace.failure_reason()
    if reason is None:
        return None
    if "ModelAdapterError" in reason or "model request" in reason:
        return FailureDiagnosis(
            FailureCategory.PROVIDER_ERROR,
            "the model provider failed and the run could not continue",
            (f"failure_reason:{reason}",),
        )
    return None


def _loop_stuck(trace: TraceRun) -> FailureDiagnosis | None:
    """Detect the same tool call repeated with no intervening state change."""
    seen: dict[str, int] = {}
    for record in trace.by_name("tool_call_completed"):
        if as_str(record.data.get("status")) != "succeeded":
            continue
        signature = f"{record.data.get('tool')}:{record.data.get('result')}"
        count = seen.get(signature, 0) + 1
        seen[signature] = count
        if count >= _LOOP_STUCK_REPEATS:
            return FailureDiagnosis(
                FailureCategory.LOOP_STUCK,
                f"the same tool call returned an identical result {count} times",
                (f"repeat:{signature[:120]}",),
            )
    return None


def _denials(trace: TraceRun | None) -> list[str]:
    """Collect denials that the agent never worked around.

    A denial the agent recovered from by using an allowed tool is *not* the cause of
    failure, so it must not be reported as one — otherwise a healthy harness would
    look broken on every run that happened to probe a boundary first.
    """
    if trace is None:
        return []
    succeeded: set[str] = {
        as_str(record.data.get("tool"))
        for record in trace.by_name("tool_call_completed")
        if as_str(record.data.get("status")) == "succeeded"
    }
    evidence: list[str] = []
    for record in trace.by_name("permission_decided"):
        if record.data.get("allowed") is not False:
            continue
        tool = as_str(record.data.get("tool"))
        if tool in succeeded:
            continue
        evidence.append(f"{tool}: {as_str(record.data.get('reason'))}")
    return evidence


def _invalid_calls(trace: TraceRun | None) -> list[str]:
    """Collect calls rejected for unknown tools or invalid arguments."""
    if trace is None:
        return []
    evidence: list[str] = []
    for record in trace.by_name("tool_call_completed"):
        if as_str(record.data.get("status")) != "invalid":
            continue
        error = record.data.get("error")
        code = as_str(error.get("code")) if isinstance(error, dict) else ""
        evidence.append(f"{record.data.get('tool')}: {code or 'invalid'}")
    return evidence


def _unresolved_tool_errors(trace: TraceRun | None) -> list[str]:
    """Collect tool failures that were never followed by a success of that tool."""
    if trace is None:
        return []
    failed: dict[str, int] = {}
    recovered: dict[str, int] = {}
    for record in trace.by_name("tool_call_completed"):
        tool = as_str(record.data.get("tool"))
        status = as_str(record.data.get("status"))
        if status == "succeeded":
            recovered[tool] = recovered.get(tool, 0) + 1
        elif status in {"failed", "timed_out"}:
            failed[tool] = failed.get(tool, 0) + 1
    evidence = [
        f"{tool}: {count} failure(s), {recovered.get(tool, 0)} success(es)"
        for tool, count in sorted(failed.items())
        if recovered.get(tool, 0) == 0
    ]
    return evidence


def _verification_detail(
    trace: TraceRun | None, verifier_exit_code: int | None
) -> tuple[FailureCategory, str, tuple[str, ...]]:
    """Distinguish a wrong change from a context-starved change."""
    evidence: list[str] = [f"verifier_exit_code:{verifier_exit_code}"]
    if trace is not None:
        omitted = sum(
            as_int(record.data.get("omitted_messages")) for record in trace.by_name("context_built")
        )
        read_tools = frozenset({"read_file", "search", "git"})
        inspected = bool(read_tools & set(trace.tool_names()))
        evidence.append(f"omitted_messages:{omitted}")
        evidence.append(f"inspected_repository:{inspected}")
        if omitted > 0 and not inspected:
            return (
                FailureCategory.CONTEXT_MISS,
                "the context strategy dropped history and the agent never re-read the repository",
                tuple(evidence),
            )
    return (
        FailureCategory.VERIFICATION_FAILED,
        "the run reported completion but the executable success criterion still fails",
        tuple(evidence),
    )


def summarize(diagnoses: list[FailureDiagnosis]) -> dict[str, int]:
    """Count diagnoses by category, including successes under ``none``."""
    counts: dict[str, int] = {}
    for diagnosis in diagnoses:
        key = diagnosis.category.value
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def summarize_layers(diagnoses: list[FailureDiagnosis]) -> dict[str, int]:
    """Count outcomes by responsible layer, which is the headline split."""
    counts = {"success": 0, "harness": 0, "agent": 0, "environment": 0}
    for diagnosis in diagnoses:
        counts[diagnosis.layer] += 1
    return counts


__all__ = [
    "FailureDiagnosis",
    "classify_failure",
    "summarize",
    "summarize_layers",
]
