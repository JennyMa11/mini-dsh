"""Full-cohort screening audit and predeclared training admission, without new calls."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from minidsh.evaluation.tracefile import TraceRun
from minidsh_rl.report import summarize
from minidsh_rl.reprice import milli_upper
from minidsh_rl.screen_v2 import screening_queue

MIN_SUCCESS = 0.2
MAX_SUCCESS = 0.8


def admission(*, declared: int, completed: int, successes: int, unknown_usage: int) -> bool:
    """Data-readiness gate, not a significance test or a claim of RL benefit."""
    return bool(
        declared
        and declared == completed
        and not unknown_usage
        and MIN_SUCCESS <= successes / declared <= MAX_SUCCESS
    )


def analyze(directory: Path) -> dict[str, object]:  # noqa: PLR0912, PLR0915 - complete evidence audit
    completed = json.loads((directory / "summary.json").read_text())
    rows = json.loads((directory / "queue.json").read_text())
    expected = screening_queue()
    if completed["completed"] != len(expected) or len(rows) != len(expected):
        raise ValueError("screening cohort incomplete; keep original denominator")
    sources = json.loads((directory / "protocol.json").read_text())["sources"]
    for file, digest in sources.items():
        if hashlib.sha256(Path(file).read_bytes()).hexdigest() != digest:
            raise ValueError("frozen screening source changed")
    hashes = {
        str(directory / name): hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in ("summary.json", "queue.json", "protocol.json")
    }
    details: list[dict[str, object]] = []
    raw_unknown = known_zero = 0
    for row, frozen in zip(rows, expected, strict=True):
        for key in ("case_id", "family", "split", "repeat", "seed"):
            if row[key] != frozen[key]:
                raise ValueError("screening declaration changed")
        run = directory / row["run_id"]
        result = json.loads((run / "result.json").read_text())
        if any(
            row[key] != result[key]
            for key in ("case_id", "split", "seed", "success", "status", "tool_calls")
        ):
            raise ValueError("queue/result mismatch")
        if type(row["success"]) is not bool:
            raise ValueError("missing independent verification outcome")
        if result["candidate_hash"]:
            if (
                hashlib.sha256((run / "candidate.py").read_bytes()).hexdigest()
                != result["candidate_hash"]
            ):
                raise ValueError("candidate hash mismatch")
            verdict = json.loads((run / "verification/result.json").read_text())
            if (
                verdict["passed"] != row["success"]
                or verdict["case_id"] != row["case_id"]
                or verdict["criterion_version"] != "expansion_v2"
            ):
                raise ValueError("independent verifier mismatch")
        elif row["success"] is not False:
            raise ValueError("success without candidate")
        trace = TraceRun.from_file(run / "trace.jsonl", run_id=row["run_id"])
        final = trace.final()
        if final is None:
            raise ValueError("unterminated Trace")
        raw_unknown += int(str(final.data.get("unknown_usage_calls", 0)))
        known_zero += sum(
            record.data.get("error") == "context_overflow: experiment token budget exhausted"
            for record in trace.by_name("model_call_failed")
        )
        turns = [json.loads(p.read_text()) for p in sorted((run / "tokens").glob("turn_*.json"))]
        i = sum(len(t["prompt_token_ids"]) for t in turns)
        o = sum(len(t["completion_token_ids"]) for t in turns)
        if i + o != row["tokens"] or i + o != result["total_tokens"]:
            raise ValueError("raw Token counters differ")
        if len(turns) != len(trace.by_name("model_call_completed")):
            raise ValueError("raw model-call coverage incomplete")
        detail: dict[str, object] = dict(
            row,
            input_tokens=i,
            output_tokens=o,
            model_calls=len(turns),
            rejected=len(trace.by_name("model_output_rejected")),
            max_sequence=max(
                len(t["prompt_token_ids"]) + len(t["completion_token_ids"]) - 1 for t in turns
            ),
            sample_upper_cny=str(
                sum(
                    (
                        milli_upper(len(t["prompt_token_ids"]), len(t["completion_token_ids"]))
                        for t in turns
                    ),
                    Decimal(0),
                )
            ),
        )
        details.append(detail)
        for p in run.rglob("*"):
            if p.is_file() and p.suffix in {".json", ".jsonl", ".py", ".txt"}:
                hashes[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    successes = sum(r["success"] is True for r in details)
    if known_zero > raw_unknown:
        raise ValueError("inconsistent unknown-usage evidence")
    unknown = raw_unknown - known_zero
    groups = {
        split: summarize([r for r in details if r["split"] == split], declared=n)
        for split, n in (("train", 96), ("validation", 32))
    }
    families = {
        family: summarize([r for r in details if r["family"] == family], declared=8)
        for family in sorted({str(r["family"]) for r in details})
    }
    by_case: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in details:
        by_case[str(row["case_id"])].append(row)
    mixed = sum(sum(r["success"] is True for r in case) == 1 for case in by_case.values())
    train = [r for r in details if r["split"] == "train"]
    val = [r for r in details if r["split"] == "validation"]
    mean_train_cost = sum((Decimal(str(r["sample_upper_cny"])) for r in train), Decimal(0)) / len(
        train
    )
    mean_val_cost = sum((Decimal(str(r["sample_upper_cny"])) for r in val), Decimal(0)) / len(val)
    group_padded = []
    for case in by_case.values():
        if case[0]["split"] == "train":
            padded = (
                2
                * sum(int(str(r["model_calls"])) for r in case)
                * max(int(str(r["max_sequence"])) for r in case)
            )
            group_padded.append(padded)
    mean_update_cost = sum(
        (milli_upper(0, 0, padded) for padded in group_padded), Decimal(0)
    ) / len(group_padded)
    estimates = {}
    for fraction in (Decimal(".4"), Decimal(".8"), Decimal("1")):
        value = (
            160 * mean_train_cost
            + 64 * mean_val_cost
            + 40 * fraction * (mean_update_cost + Decimal(".11"))
            + Decimal(".7")
        )
        estimates[str(fraction)] = str(value)
    hashes[__file__] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return {
        "schema_version": 1,
        "declared": len(rows),
        "completed": len(details),
        "successes": successes,
        "ready_for_smoke": admission(
            declared=len(rows), completed=len(details), successes=successes, unknown_usage=unknown
        ),
        "unknown_usage_calls_raw": raw_unknown,
        "known_local_pre_rpc_rejections": known_zero,
        "unknown_provider_usage_calls": unknown,
        "groups": groups,
        "families": families,
        "mixed_two_sample_cases": mixed,
        "cases": len(by_case),
        "new_test_model_calls": 0,
        "pilot_shadow_cost_scenarios_cny": estimates,
        "runs": details,
        "input_hashes": hashes,
        "limitations": [
            "two samples per case cannot estimate true success probability precisely",
            "screening is development evidence, not an RL or held-out-test result",
            "group padding projections duplicate two Base trajectories to approximate four; "
            "not measured training cost",
        ],
        "failure_types": dict(
            Counter(str(r["failure_type"]) for r in details if r["success"] is False)
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.input)
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
