"""Offline, version-isolated coverage audit for the single-seed curve dataset."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from minidsh_rl.curve_cases import curve_cases
from minidsh_rl.curve_cases_v2 import curve_cases_v2
from minidsh_rl.feedback_coverage import CoverageRow
from minidsh_rl.learning_gates import audit

DECLARED_CHECKS = 420
DECLARED_CASES = 30


def coverage_gate(rows: list[CoverageRow], *, declared: int) -> bool:
    """Both checkers must detect every declared error through executed tests."""
    return (
        bool(rows)
        and len(rows) * 2 == declared
        and all(
            check["passed"] == row["expected_correct"]
            and not check["timed_out"]
            and check["exit_code"] == (0 if row["expected_correct"] else 1)
            for row in rows
            for check in (row["public"], row["private"])
        )
    )


def summarize(source: Path, output: Path) -> None:
    """Derive a new report; preserve raw verdicts and archived executing source."""
    raw = source.read_bytes()
    record = json.loads(raw)
    for name, expected in record["verdict_artifact_sha256"].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected:
            raise ValueError("verdict evidence changed")
    if record["candidate_policy"] != "curve_pure_v1":
        raise ValueError("wrong candidate policy")
    if (
        record["declared_checks"] != DECLARED_CHECKS
        or record["completed_checks"] != DECLARED_CHECKS
        or len(record["case_bindings"]) != DECLARED_CASES
    ):
        raise ValueError("wrong frozen denominator")
    rows = record["rows"]
    admitted = coverage_gate(rows, declared=420) and record["source_unchanged"]
    misses = [
        {
            "case_id": row["case_id"],
            "probe_id": row["probe_id"],
            "checks": mode,
            "clause": row["clause"],
            "actual": row[mode],
        }
        for row in rows
        for mode in ("public", "private")
        if row[mode]["passed"] != row["expected_correct"]
        or row[mode]["timed_out"]
        or row[mode]["exit_code"] < 0
    ]
    result = {
        "schema_version": 1,
        "phase": next(iter(record["case_bindings"].values()))["dataset_version"] + "_coverage",
        "source": str(source),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "declared_checks": 420,
        "completed_checks": record["completed_checks"],
        "unique_candidate_programs": record["unique_candidate_programs"],
        "coverage": record["coverage"],
        "data_gate_passed": admitted,
        "mismatches": misses,
        "new_model_calls": 0,
        "new_training_calls": 0,
        "declared_model_trajectories": 216,
        "completed_model_trajectories": 0,
        "pending": [],
        "budget_migration_executed": False,
        "stop_reason": None if admitted else "private_or_public_coverage_failed",
        "next_stage": "not_admitted" if not admitted else "offline_scheduler_and_budget_contracts",
        "limitations": [
            "Candidate programs are reused across case/probe instances; "
            "counts are not independent samples.",
            "No new policy learning or generalization was evaluated.",
            "Coverage is limited to predeclared mutations, not exhaustive semantic correctness.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(result, stream, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-ledger", type=Path)
    parser.add_argument("--summarize", type=Path)
    parser.add_argument("--dataset", choices=("v1", "v2"), default="v1")
    args = parser.parse_args()
    if args.summarize is not None:
        summarize(args.summarize, args.output)
    else:
        if args.budget_ledger is None:
            parser.error("--budget-ledger required for audit")
        asyncio.run(
            audit(
                curve_cases() if args.dataset == "v1" else curve_cases_v2(),
                args.output,
                args.budget_ledger,
                policy="curve_pure_v1",
            )
        )


if __name__ == "__main__":
    main()
