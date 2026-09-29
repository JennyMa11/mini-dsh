"""Unit, integration, and end-to-end tests for the ablation harness."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.benchmark import (
    CONTEXT_VARIANTS,
    PERMISSION_VARIANTS,
    STANDARD_VARIANTS,
    TOOL_OUTPUT_VARIANTS,
    AblationReport,
    AblationRunner,
    BenchmarkManifest,
    BenchmarkReport,
    BenchmarkRunner,
    BenchmarkTask,
    HarnessVariant,
    Runner,
    TaskMetrics,
    VariantRegistry,
    VariantReport,
)
from minidsh.composition import LocalRuntime, RuntimeOptions, build_local_runtime
from minidsh.domain import ModelResponse, ToolCall


def _metrics(task_id: str, *, success: bool, cost: float = 0.001) -> TaskMetrics:
    return TaskMetrics(
        task_id=task_id,
        success=success,
        status="finished",
        steps=2,
        tokens=10,
        cost_usd=cost,
        tool_calls=1,
        latency_ms=5.0,
        failure_type=None if success else "verification_failed",
        verifier_exit_code=0 if success else 1,
        trace_path=f"/tmp/{task_id}.jsonl",
    )


def _report(name: str, *results: TaskMetrics) -> BenchmarkReport:
    return BenchmarkReport(name, "1.0", results or (_metrics("t1", success=True),))


def _variant_report(variant: HarnessVariant, report: BenchmarkReport) -> VariantReport:
    return VariantReport(variant, report)


class _StubRunner:
    """Stand-in runner that returns a fixed report without touching the filesystem."""

    def __init__(self, report: BenchmarkReport) -> None:
        self.report = report

    async def run(self, manifest: BenchmarkManifest) -> BenchmarkReport:
        del manifest
        return self.report


@pytest.fixture
def manifest(tmp_path: Path) -> BenchmarkManifest:
    workspace = tmp_path / "fixture"
    workspace.mkdir()
    (workspace / "value.txt").write_text("broken\n", encoding="utf-8")
    return BenchmarkManifest(
        "ablation-smoke",
        "1.0",
        (BenchmarkTask("t1", "goal", workspace, (sys.executable, "-c", "pass")),),
    )


# --- unit ---------------------------------------------------------------------


def test_harness_variant_defaults_describe_the_baseline_harness() -> None:
    variant = HarnessVariant("base")

    assert variant.context_strategy == "full"
    assert variant.reflection_enabled is False
    assert variant.detailed_tool_feedback is True
    assert variant.permission_profile == "workspace_default_deny"
    assert variant.tool_output_limit == 20_000
    assert variant.artifact_offload is True
    assert variant.max_retries == 1


def test_registry_exposes_the_documented_suites() -> None:
    registry = VariantRegistry()

    assert registry.names == ("context", "local_first", "permission", "standard", "tool_output")
    assert registry.get("standard") == STANDARD_VARIANTS


def test_registry_rejects_an_unknown_suite() -> None:
    with pytest.raises(ValueError, match="unknown ablation suite"):
        VariantRegistry().get("does-not-exist")


def test_variant_names_are_unique_within_every_suite() -> None:
    for suite in (STANDARD_VARIANTS, CONTEXT_VARIANTS, TOOL_OUTPUT_VARIANTS, PERMISSION_VARIANTS):
        names = [variant.name for variant in suite]
        assert len(names) == len(set(names))


def test_context_suite_covers_all_six_strategies_and_changes_nothing_else() -> None:
    assert {variant.context_strategy for variant in CONTEXT_VARIANTS} == {
        "full",
        "sliding",
        "summary",
        "retrieval",
        "retrieval_summary",
        "symbol",
    }
    assert all(variant.reflection_enabled is False for variant in CONTEXT_VARIANTS)
    assert all(variant.tool_output_limit == 20_000 for variant in CONTEXT_VARIANTS)


def test_tool_output_suite_varies_one_variable_at_a_time() -> None:
    unbounded, bounded, offloaded = TOOL_OUTPUT_VARIANTS

    assert unbounded.tool_output_limit == 200_000
    assert bounded.tool_output_limit == 20_000
    assert bounded.artifact_offload is False
    assert offloaded.tool_output_limit == bounded.tool_output_limit
    assert offloaded.artifact_offload is True
    assert bounded != offloaded


def test_permission_suite_varies_retry_and_feedback_separately() -> None:
    assert {variant.max_retries for variant in PERMISSION_VARIANTS} == {0, 1}
    assert {variant.detailed_tool_feedback for variant in PERMISSION_VARIANTS} == {False, True}


def test_ablation_report_serializes_configuration_and_metrics() -> None:
    report = AblationReport(
        "suite",
        (
            _variant_report(HarnessVariant("full_history"), _report("full_history")),
            _variant_report(
                HarnessVariant("repo_retrieval", context_strategy="retrieval"),
                _report("repo_retrieval", _metrics("t1", success=False)),
            ),
        ),
    )

    payload = json.loads(report.to_json())

    assert payload["manifest"] == "suite"
    assert [item["configuration"]["name"] for item in payload["variants"]] == [
        "full_history",
        "repo_retrieval",
    ]
    assert payload["variants"][0]["aggregate"]["success_rate"] == 1.0
    assert payload["variants"][1]["aggregate"]["success_rate"] == 0.0
    assert payload["variants"][1]["failure_types"] == {"verification_failed": 1}

    lines = report.to_jsonl().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["variant"] == "full_history"
    assert json.loads(lines[0])["success_rate"] == 1.0


def test_comparison_table_has_one_row_per_variant() -> None:
    report = AblationReport(
        "suite",
        tuple(_variant_report(variant, _report(variant.name)) for variant in CONTEXT_VARIANTS),
    )

    markdown = report.to_markdown()
    comparison_section = markdown.split("## Pareto trade-off analysis")[0]
    rows = [
        line
        for line in comparison_section.splitlines()
        if line.startswith("|") and not line.startswith("| ---") and "Variant |" not in line
    ]

    assert len(rows) == len(CONTEXT_VARIANTS)
    assert "| full_history |" in markdown
    assert "| symbol_retrieval |" in markdown


def test_markdown_always_carries_the_trade_off_section() -> None:
    report = AblationReport(
        "suite",
        tuple(_variant_report(variant, _report(variant.name)) for variant in CONTEXT_VARIANTS),
    )

    markdown = report.to_markdown()

    assert "## Pareto trade-off analysis" in markdown
    assert "### Success rate vs Tokens" in markdown
    assert "### Success rate vs Cost (USD)" in markdown
    assert "### Success rate vs Steps" in markdown


# --- integration --------------------------------------------------------------


@pytest.mark.asyncio
async def test_ablation_runs_every_variant_once_in_order(manifest: BenchmarkManifest) -> None:
    seen: list[str] = []

    def factory(variant: HarnessVariant) -> Runner:
        seen.append(variant.name)
        return _StubRunner(_report(variant.name))

    variants = (HarnessVariant("a"), HarnessVariant("b", context_strategy="retrieval"))
    report = await AblationRunner(factory).run(manifest, variants)

    assert seen == ["a", "b"]
    assert [item.variant.name for item in report.variants] == ["a", "b"]
    assert report.manifest_name == "ablation-smoke"


@pytest.mark.asyncio
async def test_ablation_rejects_duplicate_variant_names(manifest: BenchmarkManifest) -> None:
    runner = AblationRunner(lambda variant: _StubRunner(_report(variant.name)))

    with pytest.raises(ValueError, match="unique"):
        await runner.run(manifest, (HarnessVariant("dup"), HarnessVariant("dup")))


@pytest.mark.asyncio
async def test_ablation_keeps_every_variant_against_the_same_manifest(
    manifest: BenchmarkManifest,
) -> None:
    manifests: list[str] = []

    class _RecordingRunner(_StubRunner):
        async def run(self, recorded: BenchmarkManifest) -> BenchmarkReport:
            manifests.append(recorded.name)
            return await super().run(recorded)

    report = await AblationRunner(lambda variant: _RecordingRunner(_report(variant.name))).run(
        manifest, STANDARD_VARIANTS
    )

    assert manifests == [manifest.name] * len(STANDARD_VARIANTS)
    assert len(report.variants) == len(STANDARD_VARIANTS)


# --- end to end ---------------------------------------------------------------


def _scripted_responses() -> list[ModelResponse]:
    return [
        ModelResponse(
            tool_calls=(
                ToolCall(
                    "patch",
                    "patch_file",
                    {"path": "value.txt", "expected": "broken", "replacement": "fixed"},
                ),
            )
        ),
        ModelResponse(content="done"),
    ]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_ablation_end_to_end_compares_two_context_strategies(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "value.txt").write_text("broken\n", encoding="utf-8")
    (fixture / "notes.md").write_text("value.txt is patched by the agent\n", encoding="utf-8")
    task = BenchmarkTask(
        "repair-value",
        "change broken to fixed",
        fixture,
        (
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "raise SystemExit(Path('value.txt').read_text() != 'fixed\\n')",
        ),
    )
    manifest = BenchmarkManifest("ablation-e2e", "1.0", (task,))

    def factory(variant: HarnessVariant) -> BenchmarkRunner:
        def build(workspace: Path, benchmark_task: BenchmarkTask) -> LocalRuntime:
            del benchmark_task
            return build_local_runtime(
                workspace=workspace,
                model=ScriptedModel(_scripted_responses()),
                options=RuntimeOptions(
                    allow_write=True,
                    context_strategy=variant.context_strategy,
                ),
            )

        return BenchmarkRunner(
            build,
            tmp_path / f"out-{variant.name}",
            verification_allowlist=frozenset({Path(sys.executable).name}),
        )

    variants = (
        HarnessVariant("full_history"),
        HarnessVariant("repo_retrieval", context_strategy="retrieval"),
    )
    report = await AblationRunner(factory).run(manifest, variants)

    assert [item.report.aggregate()["success_rate"] for item in report.variants] == [1.0, 1.0]
    for item in report.variants:
        output = tmp_path / f"out-{item.variant.name}"
        assert await asyncio.to_thread((output / "results.jsonl").is_file)
        assert await asyncio.to_thread((output / "traces" / "repair-value.jsonl").is_file)

    assert "| full_history |" in report.to_markdown()
    assert "| repo_retrieval |" in report.to_markdown()


def test_base_variant_matches_the_runtime_defaults() -> None:
    """The ablation baseline must be the default harness, or the comparison is invalid."""
    base = HarnessVariant("base")
    defaults = RuntimeOptions()

    assert base.context_strategy == defaults.context_strategy
    assert base.reflection_enabled == defaults.reflection_enabled
    assert base.detailed_tool_feedback == defaults.detailed_tool_feedback
    assert base.tool_output_limit == defaults.tool_output_limit
    assert base.artifact_offload == defaults.artifact_offload
    assert base.max_retries == defaults.tool_max_retries
