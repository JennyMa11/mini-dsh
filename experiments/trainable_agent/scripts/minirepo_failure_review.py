"""Audit observable failure stages in closed cohorts; no model or network calls."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from minidsh_rl.minirepo_studies import WORKFLOW, study_layout

Outcome = Literal[
    "success",
    "candidate_unavailable",
    "no_patch_step_budget",
    "no_patch_other",
    "patched_policy_rejected",
    "patched_incorrect_step_budget",
    "patched_incorrect_other",
]
DECLARED = 32


def classify(
    *, success: bool, candidate: bool, patches: int, policy_rejected: bool, step_budget: bool
) -> Outcome:
    """Partition observable outcomes, without claiming causal failure reasons."""
    if success:
        return "success"
    if not candidate:
        return "candidate_unavailable"
    if patches == 0:
        return "no_patch_step_budget" if step_budget else "no_patch_other"
    if policy_rejected:
        return "patched_policy_rejected"
    return "patched_incorrect_step_budget" if step_budget else "patched_incorrect_other"


def review(directory: Path) -> dict[str, object]:  # noqa: PLR0912, PLR0915
    """Bind all labels to raw records and retain every declared attempt."""
    inputs: dict[str, str] = {}

    def read(path: Path) -> Any:
        contents = path.read_bytes()
        inputs[str(path)] = hashlib.sha256(contents).hexdigest()
        return json.loads(contents)

    queue = read(directory / "queue.json")
    protocol = read(directory / "protocol.json")
    if not isinstance(queue, list) or not isinstance(protocol, dict):
        raise ValueError("invalid queue or protocol")
    if len(queue) != DECLARED or protocol["declared"] != DECLARED:
        raise ValueError("full 32-attempt cohort required")
    layout = study_layout(str(protocol.get("phase", WORKFLOW.phase)))
    key, arms = layout.comparison_key, layout.arms
    arm_comparison = key != "mode"
    records = []
    counters: dict[str, Counter[str]] = {m: Counter() for m in arms}
    seen = set()
    for row in queue:
        if row["status"] != "completed" or row["run_id"] in seen:
            raise ValueError("incomplete or duplicate attempt")
        seen.add(row["run_id"])
        root = directory / row["run_id"]
        result = read(root / "result.json")
        operations = read(root / "operations.json")
        if not isinstance(result, dict) or not isinstance(operations, list):
            raise ValueError("invalid result or operations")
        if any(result[k] != row[k] for k in ("case_id", "mode", "seed", "run_id")):
            raise ValueError("queue identity mismatch")
        candidate_path = root / "candidate.json"
        verdict = read(root / "verification/result.json") if candidate_path.exists() else None
        if candidate_path.exists():
            candidate = read(candidate_path)
            digest = hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()
            if not isinstance(verdict, dict) or (
                verdict["candidate_sha256"] != digest
                or result["candidate_hash"] != digest
                or verdict["passed"] != result["success"]
            ):
                raise ValueError("candidate/verdict identity mismatch")
        trace = root / "trace.jsonl"
        contents = trace.read_bytes()
        inputs[str(trace)] = hashlib.sha256(contents).hexdigest()
        events = [json.loads(line) for line in contents.splitlines()]
        if any(event["run_id"] != row["run_id"] for event in events):
            raise ValueError("trace identity mismatch")
        rejected = sum(e["name"] == "model_output_rejected" for e in events)
        cap_stops = 0
        files = sorted((root / "tokens").glob("turn_*.json"))
        for i, path in enumerate(files):
            raw = read(path)
            if (
                not isinstance(raw, dict)
                or raw["run_id"] != row["run_id"]
                or raw["call_index"] != i
            ):
                raise ValueError("sample identity mismatch")
            cap_stops += (
                raw["stop_reason"] == "length"
                and len(raw["completion_token_ids"]) == protocol["max_completion_per_call"]
            )
        if len(files) != result["sampled_calls"]:
            raise ValueError("missing raw calls")
        policy_rejected = isinstance(verdict, dict) and verdict["exit_code"] == -1
        outcome = classify(
            success=result["success"],
            candidate=candidate_path.exists(),
            patches=result["patches"],
            policy_rejected=policy_rejected,
            step_budget=result["loop_failure_reason"] == "step budget exhausted",
        )
        public_after_patch = sum(x["operation"] == "test" and x["patches"] > 0 for x in operations)
        failed_after_patch = sum(
            x["operation"] == "test" and x["patches"] > 0 and x["exit_code"] not in (0, None)
            for x in operations
        )
        counts = counters[row[key]]
        counts[outcome] += 1
        counts["declared"] += 1
        counts["runs_with_format_rejection"] += rejected > 0
        counts["runs_with_output_cap_stop"] += cap_stops > 0
        counts["runs_with_public_test_after_patch"] += public_after_patch > 0
        counts["runs_with_failed_public_test_after_patch"] += failed_after_patch > 0
        records.append(
            {
                "run_id": row["run_id"],
                "case_id": row["case_id"],
                "mode": row["mode"],
                "comparison_arm": row[key],
                "outcome": outcome,
                "patches": result["patches"],
                "format_rejections": rejected,
                "exact_output_cap_stops": cap_stops,
                "public_tests_after_patch": public_after_patch,
                "failed_public_tests_after_patch": failed_after_patch,
                "evidence_root": str(root),
            }
        )
    if any(c["declared"] != DECLARED // 2 for c in counters.values()):
        raise ValueError("unbalanced mode denominator")
    return {
        "schema_version": 1,
        "phase": "post_hoc_observable_failure_review",
        "declared": DECLARED,
        "checked": len(records),
        "comparison_key": key,
        "by_arm" if arm_comparison else "by_mode": {m: dict(c) for m, c in counters.items()},
        "runs": records,
        "input_sha256": inputs,
        "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limitations": [
            "Outcome classes describe observed stages, not causal failure reasons.",
            "Format and cap flags overlap with outcome classes and each other.",
            "No semantic reasoning attribution or tool-choice accuracy was assessed.",
            "Missing public feedback after a patch is expected in single-submission mode.",
            "No new model calls or reruns; raw traces and original verdicts remain unchanged.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = review(args.directory)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)


if __name__ == "__main__":
    main()
