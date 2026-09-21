"""Single-variable harness experiment orchestration."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Protocol

from minidsh.benchmark.runner import BenchmarkManifest, BenchmarkReport
from minidsh.evaluation import pareto
from minidsh.evaluation.pareto import MetricPoint, ParetoAnalysis


@dataclass(frozen=True, slots=True)
class HarnessVariant:
    """Named harness configuration recorded alongside benchmark results."""

    name: str
    context_strategy: str = "full"
    reflection_enabled: bool = False
    detailed_tool_feedback: bool = True
    permission_profile: str = "workspace_default_deny"
    tool_output_limit: int = 20_000
    artifact_offload: bool = True
    max_retries: int = 1


@dataclass(frozen=True, slots=True)
class VariantReport:
    """One variant and its benchmark report."""

    variant: HarnessVariant
    report: BenchmarkReport


@dataclass(frozen=True, slots=True)
class AblationReport:
    """Comparable reports produced against one unchanged task manifest."""

    manifest_name: str
    variants: tuple[VariantReport, ...]

    def to_json(self) -> str:
        """Serialize variant configuration and aggregate/task metrics."""
        return json.dumps(
            {
                "manifest": self.manifest_name,
                "pareto": [item.to_json() for item in self.pareto()],
                "variants": [
                    {
                        "configuration": asdict(item.variant),
                        "aggregate": item.report.aggregate(),
                        "failure_categories": item.report.failure_category_counts(),
                        "failure_layers": item.report.failure_layer_counts(),
                        "failure_types": item.report.failure_type_counts(),
                        "results": [asdict(result) for result in item.report.results],
                    }
                    for item in self.variants
                ],
            },
            indent=2,
            sort_keys=True,
        )

    def points(self) -> tuple[MetricPoint, ...]:
        """Reduce every variant to the four comparable metrics."""
        return tuple(
            MetricPoint.from_aggregate(item.variant.name, item.report.aggregate())
            for item in self.variants
        )

    def pareto(self) -> tuple[ParetoAnalysis, ...]:
        """Return the trade-off analysis across every cost axis."""
        return pareto.analyze(self.points())

    def to_jsonl(self) -> str:
        """Serialize one variant summary record per line."""
        records = []
        for item in self.variants:
            aggregate = item.report.aggregate()
            records.append(
                json.dumps(
                    {
                        "variant": item.variant.name,
                        "manifest": self.manifest_name,
                        "configuration": asdict(item.variant),
                        **aggregate,
                    },
                    sort_keys=True,
                )
            )
        return "".join(record + "\n" for record in records)

    def to_markdown(self) -> str:
        """Render a variant comparison table plus the Pareto trade-off section."""
        lines = [
            f"# Ablation report: {self.manifest_name}",
            "",
            "| Variant | Context | Reflection | Feedback | Success | Pass@k | Steps |"
            " Tokens | Cost (USD) | Cost/success | Retry OK | Denied | Blocked |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for item in self.variants:
            aggregate = item.report.aggregate()
            lines.append(
                f"| {item.variant.name} | {item.variant.context_strategy} | "
                f"{'on' if item.variant.reflection_enabled else 'off'} | "
                f"{'detailed' if item.variant.detailed_tool_feedback else 'basic'} | "
                f"{aggregate['success_rate']:.0%} | {aggregate['pass_at_k']:.0%} | "
                f"{aggregate['average_steps']:.2f} | "
                f"{aggregate['average_tokens']:.1f} | {aggregate['total_cost_usd']:.6f} | "
                f"{aggregate['cost_per_success_usd']:.6f} | "
                f"{aggregate['retry_success_rate']:.0%} | {aggregate['permission_denied']} | "
                f"{aggregate['unsafe_blocked']} |"
            )
        lines += ["", pareto.to_markdown(self.points(), self.pareto()).rstrip(), ""]
        return "\n".join(lines) + "\n"


class Runner(Protocol):
    """Anything that can execute one manifest and return a comparable report."""

    async def run(self, manifest: BenchmarkManifest) -> BenchmarkReport:
        """Execute the manifest and return its measured outcomes."""


type RunnerFactory = Callable[[HarnessVariant], Runner]


class AblationRunner:
    """Run the same manifest once for each explicitly configured variant."""

    def __init__(self, runner_factory: RunnerFactory) -> None:
        self._runner_factory = runner_factory

    async def run(
        self, manifest: BenchmarkManifest, variants: tuple[HarnessVariant, ...]
    ) -> AblationReport:
        """Execute variants sequentially to reduce resource interference."""
        if len({variant.name for variant in variants}) != len(variants):
            msg = "ablation variant names must be unique"
            raise ValueError(msg)
        reports = [
            VariantReport(variant, await self._runner_factory(variant).run(manifest))
            for variant in variants
        ]
        return AblationReport(manifest.name, tuple(reports))


STANDARD_VARIANTS = (
    HarnessVariant("base_agent"),
    HarnessVariant("repo_retrieval", context_strategy="retrieval"),
    HarnessVariant("context_compaction", context_strategy="retrieval_summary"),
    HarnessVariant(
        "reflection",
        context_strategy="retrieval_summary",
        reflection_enabled=True,
    ),
    HarnessVariant(
        "better_tool_feedback",
        context_strategy="retrieval_summary",
        reflection_enabled=True,
        detailed_tool_feedback=True,
    ),
)

CONTEXT_VARIANTS = (
    HarnessVariant("full_history", context_strategy="full"),
    HarnessVariant("sliding_window", context_strategy="sliding"),
    HarnessVariant("summary_compaction", context_strategy="summary"),
    HarnessVariant("repo_retrieval", context_strategy="retrieval"),
    HarnessVariant("retrieval_summary", context_strategy="retrieval_summary"),
    HarnessVariant("symbol_retrieval", context_strategy="symbol"),
)

TOOL_OUTPUT_VARIANTS = (
    HarnessVariant("unbounded_output", tool_output_limit=200_000, artifact_offload=False),
    HarnessVariant("bounded_output", tool_output_limit=20_000, artifact_offload=False),
    HarnessVariant("bounded_output_artifact", tool_output_limit=20_000, artifact_offload=True),
)

PERMISSION_VARIANTS = (
    HarnessVariant("no_retry", max_retries=0),
    HarnessVariant("single_retry", max_retries=1),
    HarnessVariant("basic_feedback", detailed_tool_feedback=False),
    HarnessVariant("detailed_feedback", detailed_tool_feedback=True),
)


@dataclass(frozen=True, slots=True)
class VariantRegistry:
    """Named variant suites selectable from the command line."""

    suites: dict[str, tuple[HarnessVariant, ...]] = field(
        default_factory=lambda: {
            "standard": STANDARD_VARIANTS,
            "context": CONTEXT_VARIANTS,
            "tool_output": TOOL_OUTPUT_VARIANTS,
            "permission": PERMISSION_VARIANTS,
        }
    )

    def get(self, name: str) -> tuple[HarnessVariant, ...]:
        """Return a suite by name or raise ``ValueError``."""
        try:
            return self.suites[name]
        except KeyError as exc:
            msg = f"unknown ablation suite: {name}; choose from {sorted(self.suites)}"
            raise ValueError(msg) from exc

    @property
    def names(self) -> tuple[str, ...]:
        """Return the available suite names."""
        return tuple(sorted(self.suites))
