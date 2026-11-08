"""Classify saved rejected actions without model calls, repairs or new rollouts."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import cast

from minidsh.evaluation.tracefile import TraceRun
from minidsh_rl.model import parse_output


def diagnose(directories: tuple[Path, ...]) -> dict[str, object]:
    """Join rejection events to exact raw sidecars; preserve provenance and coverage."""
    items: list[dict[str, object]] = []
    hashes: dict[str, str] = {}
    calls = 0
    for directory in directories:
        for trace_path in sorted(directory.glob("*/trace.jsonl")):
            hashes[str(trace_path)] = hashlib.sha256(trace_path.read_bytes()).hexdigest()
            trace = TraceRun.from_file(trace_path)
            # A sidecar is written exactly once for each returned sample; no hidden retries.
            turns = []
            for path in sorted((trace_path.parent / "tokens").glob("turn_*.json")):
                hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
                turns.append(cast(dict[str, object], json.loads(path.read_text())))
            calls += len(turns)
            rejected = len(trace.by_name("model_output_rejected"))
            discovered = 0
            for turn in turns:
                text = str(turn["raw_text"])
                try:
                    parse_output(text, "offline_no_execution")
                except ValueError as error:
                    discovered += 1
                    cause = str(error)
                    completion = cast(list[int], turn["completion_token_ids"])
                    items.append(
                        {
                            "run_id": turn["run_id"],
                            "call_index": turn["call_index"],
                            "stop_reason": turn["stop_reason"],
                            "completion_tokens": len(completion),
                            "parse_error": cause,
                            "length_stopped": turn["stop_reason"] == "length",
                            "trace_path": str(trace_path),
                        }
                    )
            if rejected != discovered:
                raise ValueError("raw parser/rejection event mismatch: " + str(trace_path))
    if not calls:
        raise ValueError("no saved samples inspected")
    hashes[__file__] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    parser_file = Path("experiments/trainable_agent/src/minidsh_rl/model.py")
    hashes[str(parser_file)] = hashlib.sha256(parser_file.read_bytes()).hexdigest()
    return {
        "schema_version": 1,
        "scope": [str(p) for p in directories],
        "sample_calls": calls,
        "rejected_actions": len(items),
        "parse_error_counts": dict(Counter(str(x["parse_error"]) for x in items)),
        "stop_reason_counts": dict(Counter(str(x["stop_reason"]) for x in items)),
        "length_stopped_rejections": sum(x["length_stopped"] is True for x in items),
        "items": items,
        "input_hashes": hashes,
        "limitation": "parser categories and length stops are facts; neither alone proves "
        "a parser defect or the cause of semantic task failure; no model-based judging",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = diagnose(tuple(args.input))
    # Exclusive publication prevents overwriting either raw or derived evidence.
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
