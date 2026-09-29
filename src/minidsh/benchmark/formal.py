"""Frozen local-model study protocol, durable checkpoints, and paired analysis."""

from __future__ import annotations

import json
import os
import random
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from minidsh.benchmark.ablation import HarnessVariant
from minidsh.benchmark.hashes import frozen_tree_digest
from minidsh.benchmark.runner import BenchmarkError, TaskMetrics

__all__ = [
    "FORMAL_VARIANTS",
    "Checkpoint",
    "StudyKey",
    "frozen_tree_digest",
    "paired_summary",
    "schedule",
]

FORMAL_VARIANTS: tuple[HarnessVariant, ...] = (
    HarnessVariant(
        "raw_200k",
        tool_output_limit=200_000,
        normalize_tool_results=True,
        error_recovery_enabled=False,
    ),
    HarnessVariant(
        "bounded_20k",
        normalize_tool_results=True,
        error_recovery_enabled=False,
    ),
    HarnessVariant(
        "progressive_repo",
        progressive_tools_enabled=True,
        normalize_tool_results=True,
        error_recovery_enabled=False,
    ),
    HarnessVariant(
        "budget_context",
        context_strategy="budget",
        progressive_tools_enabled=True,
        normalize_tool_results=True,
        error_recovery_enabled=False,
    ),
    HarnessVariant(
        "loop_guard_recovery",
        context_strategy="budget",
        progressive_tools_enabled=True,
        normalize_tool_results=True,
        local_first_enabled=True,
        error_recovery_enabled=True,
    ),
)


@dataclass(frozen=True, slots=True)
class StudyKey:
    """Identity of one measured execution."""

    study: str
    model: str
    variant: str
    task_id: str
    repeat: int

    def slug(self) -> str:
        """Stable filesystem-safe trace name."""
        return f"{self.study}/{self.model}/{self.variant}/{self.task_id}.run{self.repeat}"


def schedule(
    task_ids: tuple[str, ...],
    *,
    repeats: int,
    models: tuple[str, ...],
    variants: tuple[str, ...],
    study: str = "matrix",
) -> tuple[StudyKey, ...]:
    """Pair variants within each task, alternating order and rotating models."""
    if repeats < 1 or len(set(task_ids)) != len(task_ids):
        raise BenchmarkError("invalid formal study schedule")
    rows: list[StudyKey] = []
    for repeat in range(repeats):
        model_order = models[repeat % len(models) :] + models[: repeat % len(models)]
        for model in model_order:
            for task_index, task_id in enumerate(task_ids):
                variant_order = variants if (task_index + repeat) % 2 == 0 else variants[::-1]
                rows.extend(
                    StudyKey(study, model, variant, task_id, repeat) for variant in variant_order
                )
    return tuple(rows)


class Checkpoint:
    """Append one fsynced result per completed run; refuse ambiguous resumes."""

    def __init__(self, path: Path, protocol_hash: str) -> None:
        self.path = path.resolve()
        self.protocol_hash = protocol_hash
        self._results: dict[StudyKey, TaskMetrics] = {}
        if self.path.exists():
            for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
                try:
                    record = json.loads(line)
                    if record["protocol_hash"] != protocol_hash:
                        raise BenchmarkError("checkpoint protocol hash mismatch")
                    key = StudyKey(**record["key"])
                    result = TaskMetrics(**record["metrics"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise BenchmarkError(f"invalid checkpoint line {number}") from exc
                if key in self._results:
                    raise BenchmarkError(f"duplicate checkpoint key: {key}")
                trace = Path(result.trace_path)
                if not trace.is_absolute():
                    trace = self.path.parent / trace
                if not trace.is_file():
                    raise BenchmarkError(f"missing checkpoint trace: {trace}")
                self._results[key] = result

    def get(self, key: StudyKey) -> TaskMetrics | None:
        """Return a completed run, if one exists."""
        return self._results.get(key)

    def items(self) -> tuple[tuple[StudyKey, TaskMetrics], ...]:
        """Return persisted runs for complete-cell report reconstruction."""
        return tuple(self._results.items())

    def append(self, key: StudyKey, result: TaskMetrics) -> None:
        """Durably append one result only after its trace exists."""
        if key in self._results:
            raise BenchmarkError(f"duplicate checkpoint key: {key}")
        trace = Path(result.trace_path)
        if not trace.is_absolute():
            trace = self.path.parent / trace
        if not trace.is_file():
            raise BenchmarkError(f"missing checkpoint trace: {trace}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": "1.0",
            "protocol_hash": self.protocol_hash,
            "key": asdict(key),
            "metrics": asdict(result),
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._results[key] = result


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def paired_summary(
    pairs: dict[str, list[tuple[TaskMetrics, TaskMetrics]]],
    *,
    seed: int = 42,
    bootstrap_samples: int = 2_000,
) -> dict[str, Any]:
    """Bootstrap task clusters, retaining repeats within each sampled task."""
    if not pairs or bootstrap_samples < 1:
        raise BenchmarkError("paired analysis needs tasks and bootstrap samples")
    task_ids = sorted(pairs)
    for task_id in task_ids:
        if not pairs[task_id]:
            raise BenchmarkError(f"task has no paired runs: {task_id}")

    def deltas(ids: list[str]) -> tuple[float, float, float]:
        token = [
            float(right.tokens - left.tokens) for task_id in ids for left, right in pairs[task_id]
        ]
        success = [
            float(right.success) - float(left.success)
            for task_id in ids
            for left, right in pairs[task_id]
        ]
        wall = [
            right.latency_ms - left.latency_ms for task_id in ids for left, right in pairs[task_id]
        ]
        return _mean(token), _mean(success), _mean(wall)

    observed_tokens, observed_success, observed_wall = deltas(task_ids)
    rng = random.Random(seed)
    draws = [deltas([rng.choice(task_ids) for _ in task_ids]) for _ in range(bootstrap_samples)]

    def interval(index: int) -> list[float]:
        ordered = sorted(item[index] for item in draws)
        return [
            ordered[int(0.025 * (len(ordered) - 1))],
            ordered[int(0.975 * (len(ordered) - 1))],
        ]

    return {
        "tasks": len(task_ids),
        "paired_runs": sum(len(items) for items in pairs.values()),
        "token_delta_mean": observed_tokens,
        "token_delta_ci95": interval(0),
        "success_delta_mean": observed_success,
        "success_delta_ci95": interval(1),
        "wall_delta_ms_mean": observed_wall,
        "wall_delta_ms_ci95": interval(2),
        "bootstrap_unit": "task",
        "bootstrap_seed": seed,
    }
