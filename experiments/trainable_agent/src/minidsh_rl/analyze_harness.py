"""Audit the frozen development comparison and apply its predeclared selection rule."""

import argparse
import hashlib
import json
from pathlib import Path

from minidsh.evaluation.tracefile import TraceRun
from minidsh_rl.harness_comparison import development_queue
from minidsh_rl.report import summarize


def choose(  # noqa: PLR0913 - explicit predeclared sufficient statistics
    *,
    complete: bool,
    original_success: int,
    candidate_success: int,
    original_rejected: int,
    original_calls: int,
    candidate_rejected: int,
    candidate_calls: int,
) -> str | None:
    """Development-only rule; incomplete evidence cannot select the next Harness."""
    if not complete or not original_calls or not candidate_calls:
        return None
    if (
        original_rejected
        and candidate_success >= original_success
        and candidate_rejected * original_calls * 10 <= 7 * original_rejected * candidate_calls
    ):
        return "json_newlines"
    return "original"


def analyze(directory: Path) -> dict[str, object]:  # noqa: PLR0912 - evidence audit boundary
    """Require all rows and cross-check raw evidence without changing any running artifact."""
    completed = json.loads((directory / "summary.json").read_text())
    rows = json.loads((directory / "queue.json").read_text())
    declared_count = len(development_queue())
    if completed["completed"] != declared_count or len(rows) != declared_count:
        raise ValueError("development queue incomplete")
    sources = json.loads((directory / "protocol.json").read_text())["sources"]
    for file, digest in sources.items():
        if hashlib.sha256(Path(file).read_bytes()).hexdigest() != digest:
            raise ValueError("frozen comparison source changed")
    details = []
    for row, expected in zip(rows, development_queue(), strict=True):
        for key in ("case_id", "variant", "seed"):
            if row[key] != expected[key]:
                raise ValueError("frozen queue changed")
        run = directory / row["run_id"]
        result = json.loads((run / "result.json").read_text())
        if any(row[k] != result[k] for k in ("case_id", "seed", "success", "status", "tool_calls")):
            raise ValueError("result/queue mismatch")
        trace = TraceRun.from_file(run / "trace.jsonl", run_id=row["run_id"])
        if trace.final() is None:
            raise ValueError("unterminated trace")
        if result["candidate_hash"]:
            if (
                hashlib.sha256((run / "candidate.py").read_bytes()).hexdigest()
                != result["candidate_hash"]
            ):
                raise ValueError("candidate hash mismatch")
            verdict = json.loads((run / "verification/result.json").read_text())
            if verdict["passed"] != row["success"]:
                raise ValueError("independent verdict mismatch")
        elif row["success"] is not False:
            raise ValueError("success without candidate")
        turns = [json.loads(p.read_text()) for p in (run / "tokens").glob("turn_*.json")]
        tokens = sum(len(t["prompt_token_ids"]) + len(t["completion_token_ids"]) for t in turns)
        if tokens != row["tokens"] or tokens != result["total_tokens"]:
            raise ValueError("raw token reconciliation failed")
        details.append(
            dict(row, model_calls=len(turns), rejected=len(trace.by_name("model_output_rejected")))
        )
    groups = {}
    for name in ("original", "json_newlines"):
        subset = [r for r in details if r["variant"] == name]
        groups[name] = dict(
            summarize(subset, declared=24),
            model_calls=sum(r["model_calls"] for r in subset),
            rejected=sum(r["rejected"] for r in subset),
        )
    a, b = groups["original"], groups["json_newlines"]
    selected = choose(
        complete=True,
        original_success=int(str(a["successes"])),
        candidate_success=int(str(b["successes"])),
        original_rejected=int(str(a["rejected"])),
        original_calls=int(str(a["model_calls"])),
        candidate_rejected=int(str(b["rejected"])),
        candidate_calls=int(str(b["model_calls"])),
    )
    return {
        "schema_version": 1,
        "scope": "development_only_not_new_test_or_RL_effect",
        "selected": selected,
        "groups": groups,
        "runs": details,
        "source": "frozen queue, result, independent verifier, raw token sidecars and Trace",
        "source_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "comparison_protocol_hash": hashlib.sha256(
            (directory / "protocol.json").read_bytes()
        ).hexdigest(),
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
