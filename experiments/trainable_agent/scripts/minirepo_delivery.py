"""Rebuild the public development report and hash manifest from closed evidence."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path("benchmark-results/trainable_agent/minirepo-v1")
DOCS = Path("docs/trainable-agent-rl")
DECLARED = 32
CLOUD_RUNS = 6
TASK_CHECKS = 56


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(quality: Path, output: Path, public: Path) -> None:  # noqa: PLR0912, PLR0915
    """Verify input bindings before emitting a distinct, reproducible derived view."""
    inputs: dict[str, str] = {}

    def read(path: Path) -> Any:
        inputs[str(path)] = digest(path)
        return json.loads(path.read_text())

    checks = read(quality)
    if not checks["checks"] or any(c["exit_code"] != 0 for c in checks["checks"]):
        raise ValueError("quality gates incomplete")
    expected = {
        "ruff_format",
        "ruff_check",
        "root_mypy",
        "experiment_mypy",
        "root_pytest",
        "experiment_pytest",
        "diff_check",
    }
    if {c["name"] for c in checks["checks"]} != expected:
        raise ValueError("missing named quality gate")
    decision = read(ROOT / "advance-decision-01.json")
    ledger = Path("benchmark-results/trainable_agent/expansion-v2/calibrated-budget.json")
    if digest(ledger) != decision["ledger_sha256"]:
        raise ValueError("budget changed since admission decision")
    inputs[str(ledger)] = digest(ledger)
    comparison = read(ROOT / "cap-comparison-01.json")
    cloud = read(ROOT / "cloud-readback-final.json")
    gates = read(ROOT / "gates-06/summary.json")
    if not gates["passed"] or gates["completed"] != TASK_CHECKS:
        raise ValueError("task gates incomplete")
    for name, frozen in gates["source_sha256"].items():
        if digest(Path(name)) != frozen:
            raise ValueError(f"task gate source changed: {name}")
        inputs[name] = frozen
    cohorts = []
    for cap, suffix, directory in (
        (512, "01", "base-interaction-01"),
        (1024, "cap1024-01", "base-interaction-cap1024-01"),
    ):
        summary = read(ROOT / directory / "summary.json")
        analysis = read(ROOT / f"analysis-{suffix}.json")
        diagnostics = read(ROOT / f"diagnostics-{suffix}.json")
        failures = read(ROOT / f"failure-review-cap{cap}-01.json")
        if summary["completed"] != DECLARED or not summary["source_unchanged"]:
            raise ValueError("incomplete or source-mutated cohort")
        if analysis["interaction_signal_gate"] or failures["checked"] != DECLARED:
            raise ValueError("stop decision or failure coverage mismatch")
        for report in (analysis, diagnostics, failures):
            if not report["input_sha256"]:
                raise ValueError("missing evidence bindings")
            for name, frozen in report["input_sha256"].items():
                if digest(Path(name)) != frozen:
                    raise ValueError(f"changed evidence: {name}")
                inputs[name] = frozen
        cohorts.append(
            {
                "max_completion_per_call": cap,
                "declared": analysis["declared"],
                "completed": analysis["completed"],
                "policies": analysis["policies"],
                "paired": analysis["paired"],
                "discordance": analysis["discordance"],
                "revision_cases": analysis["revision_cases"],
                "interaction_signal_gate": analysis["interaction_signal_gate"],
                "diagnostics": {
                    m: {k: v for k, v in p.items() if k != "runs"}
                    for m, p in diagnostics["policies"].items()
                },
                "failure_stages": failures["by_mode"],
            }
        )
    if not comparison["single_variable_protocol_passed"] or (
        cloud["visibility"] != "PRIVATE"
        or len(cloud["runs"]) != CLOUD_RUNS
        or any(r["state"] != "FINISHED" for r in cloud["runs"])
    ):
        raise ValueError("source comparison or cloud readback incomplete")
    if decision["allow_training"] or decision["allow_test"] or decision["pending_operations"]:
        raise ValueError("not a closed diagnostic stage")
    source_docs = [
        DOCS / "minirepo-plan.md",
        DOCS / "minirepo-results.md",
        DOCS / "tool-cold-start-plan.md",
        DOCS / "tool-cold-start-example.md",
        Path("docs/adr/0036-minirepo-interaction-experiment.md"),
    ]
    inputs.update({str(path): digest(path) for path in source_docs})
    report = {
        "schema_version": 1,
        "generated_by": str(Path(__file__)),
        "date": "2026-11-07",
        "phase": "development_interaction_diagnostic",
        "tasks": 8,
        "families": 4,
        "declared": 64,
        "completed": 64,
        "new_training_updates": 0,
        "new_test_opened": False,
        "cohorts": cohorts,
        "output_cap_comparison": {
            k: v for k, v in comparison.items() if k not in ("input_sha256", "scorer_sha256")
        },
        "budget": {
            k: decision[k]
            for k in (
                "shared_cumulative_limit_cny",
                "current_upper_cny",
                "remaining_cny",
                "new_diagnostic_upper_cny",
                "required_full_pilot_eval_reserve_cny",
                "budget_gate",
                "pending_operations",
                "actual_provider_bill_cny",
            )
        },
        "swanlab": cloud,
        "coverage": [
            {
                "item": "task reference/broken/mutation/public checks",
                "status": "verified",
                "evidence": str(ROOT / "gates-06/summary.json"),
            },
            {
                "item": "raw run/token/candidate/verdict identity",
                "status": "verified_64_runs",
                "evidence": [
                    str(ROOT / "analysis-01.json"),
                    str(ROOT / "analysis-cap1024-01.json"),
                ],
            },
            {
                "item": "single-variable output cap",
                "status": "verified",
                "evidence": str(ROOT / "cap-comparison-01.json"),
            },
            {
                "item": "failure stages",
                "status": "post_hoc_observation_only",
                "evidence": [str(ROOT / f"failure-review-cap{c}-01.json") for c in (512, 1024)],
            },
            {"item": "multi-turn benefit", "status": "not_demonstrated"},
            {"item": "new task RL / held-out generalization", "status": "not_run"},
            {"item": "cold-start prompt injection / example execution", "status": "design_only"},
            {
                "item": "full Memory/Skills/resume/secret/network acceptance",
                "status": "not_retested",
            },
        ],
        "quality": checks,
        "limitations": [
            "Four development families; no new RL efficacy or held-out comparison.",
            "Degenerate [0,0] bootstrap interval does not establish equivalence.",
            "Observed failure stages are not causal attributions.",
            "Budget estimates are not a reconciled provider invoice.",
        ],
        "next_stage": {
            "protocol": str(DOCS / "tool-cold-start-plan.md"),
            "status": "not_admitted",
            "paid_calls_started": 0,
        },
        "input_sha256": inputs,
        "generator_sha256": digest(Path(__file__)),
    }
    if public.exists() or output.exists():
        raise FileExistsError("new report and manifest paths required")
    public.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    evidence = {str(path): digest(path) for path in ROOT.rglob("*") if path.is_file()}
    evidence[str(public)] = digest(public)
    evidence.update(inputs)
    output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_by": str(Path(__file__)),
                "generator_sha256": digest(Path(__file__)),
                "sha256": evidence,
                "scope": "closed minirepo evidence and linked delivery inputs; excludes self",
            },
            indent=2,
        )
        + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quality", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--public", type=Path, required=True)
    args = parser.parse_args()
    generate(args.quality, args.manifest, args.public)


if __name__ == "__main__":
    main()
