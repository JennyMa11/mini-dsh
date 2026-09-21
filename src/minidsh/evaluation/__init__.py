"""Read-only evaluation views derived from runtime traces.

Nothing in this package mutates state or calls a model: every function takes a trace
(or an aggregate mapping) and returns a derived view that can be regenerated at any
time. That is what keeps the raw JSONL trace the single fact source.
"""

from minidsh.evaluation.diff import StepComparison, TraceDiff, diff_traces
from minidsh.evaluation.latency import (
    ToolLatency,
    latency_from_trace,
    retried_tool_calls,
    tool_latencies,
)
from minidsh.evaluation.pareto import (
    X_METRICS,
    Dominance,
    MetricPoint,
    ParetoAnalysis,
    analyze,
    pareto_analysis,
)
from minidsh.evaluation.replay import Replay, ReplayStep, ReplayToolCall, build_replay
from minidsh.evaluation.taxonomy import FailureDiagnosis, classify_failure, summarize_layers
from minidsh.evaluation.tracefile import TraceError, TraceRecord, TraceRun, TraceStep

__all__ = [
    "X_METRICS",
    "Dominance",
    "FailureDiagnosis",
    "MetricPoint",
    "ParetoAnalysis",
    "Replay",
    "ReplayStep",
    "ReplayToolCall",
    "StepComparison",
    "ToolLatency",
    "TraceDiff",
    "TraceError",
    "TraceRecord",
    "TraceRun",
    "TraceStep",
    "analyze",
    "build_replay",
    "classify_failure",
    "diff_traces",
    "latency_from_trace",
    "pareto_analysis",
    "retried_tool_calls",
    "summarize_layers",
    "tool_latencies",
]
