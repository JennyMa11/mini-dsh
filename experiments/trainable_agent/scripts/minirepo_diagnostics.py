"""Derive bounded diagnostics from frozen traces; no network/model calls."""

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from minidsh_rl.minirepo_studies import WORKFLOW, study_layout
from minidsh_rl.reprice import milli_upper

DECLARED = 32


def diagnose(directory: Path) -> dict[str, object]:  # noqa: PLR0912, PLR0915
    queue = json.loads((directory / "queue.json").read_text())
    if len(queue) != DECLARED or any(r["status"] != "completed" for r in queue):
        raise ValueError("complete frozen cohort required")
    protocol = json.loads((directory / "protocol.json").read_text())
    layout = study_layout(str(protocol.get("phase", WORKFLOW.phase)))
    key, arms = layout.comparison_key, layout.arms
    by_mode = {}
    inputs = {}
    for mode in arms:
        counts: Counter[str] = Counter()
        errors: Counter[str] = Counter()
        terminals: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        spending = Decimal(0)
        run_rows = []
        selected = [r for r in queue if r[key] == mode]
        if len(selected) != DECLARED // 2:
            raise ValueError("unbalanced declared comparison arms")
        for row in selected:
            root = directory / row["run_id"]
            result = json.loads((root / "result.json").read_text())
            terminals[result["status"]] += 1
            reasons[str(result["loop_failure_reason"])] += 1
            counts["successes"] += bool(result["success"])
            counts["runs_with_patch"] += result["patches"] > 0
            counts["patches"] += result["patches"]
            rejected = 0
            for path in (root / "tokens").glob("turn_*.json"):
                raw = json.loads(path.read_text())
                counts["sampled_calls"] += 1
                size = len(raw["completion_token_ids"])
                counts["input_tokens"] += len(raw["prompt_token_ids"])
                counts["output_tokens"] += size
                counts["length_stops"] += raw["stop_reason"] == "length"
                counts["patch_mentions_at_output_cap"] += (
                    raw["stop_reason"] == "length" and "apply_patch" in raw["raw_text"]
                )
                spending += milli_upper(len(raw["prompt_token_ids"]), size, 0)
            seen_reads: dict[str, str] = {}
            starts = {}
            prior_failed = None
            path = root / "trace.jsonl"
            for line in path.read_text().splitlines():
                event = json.loads(line)
                data = event["data"]
                if event["name"] == "model_output_rejected":
                    rejected += 1
                    counts["format_rejections"] += 1
                if event["name"] == "tool_call_started":
                    starts[data["call_id"]] = data
                if event["name"] == "tool_call_completed":
                    counts["tool_completions"] += 1
                    tool = data.get("tool", "unknown")
                    error = data.get("error")
                    if error:
                        errors[error.get("code", "unknown")] += 1
                    if data.get("status") == "denied":
                        counts["permission_denied"] += 1
                    payload = data.get("result")
                    started = starts.get(data["call_id"])
                    if started:
                        counts["executed_calls"] += 1
                        if tool == "apply_patch" and not error:
                            seen_reads.clear()
                        if tool == "run_tests" and isinstance(payload, dict):
                            counts["public_test_calls"] += 1
                            counts["public_test_failures"] += payload.get("exit_code") not in (
                                0,
                                None,
                            )
                        if (
                            tool == "read_file"
                            and not error
                            and isinstance(payload, dict)
                            and not payload.get("truncated")
                        ):
                            counts["checked_reads"] += 1
                            name = started["arguments"]["path"]
                            content = str(payload.get("content"))
                            counts["repeat_read_proxy"] += seen_reads.get(name) == content
                            seen_reads[name] = content
                        if tool == "read_files" and not error and isinstance(payload, dict):
                            counts["batch_read_calls"] += 1
                            counts["batch_files_requested"] += len(started["arguments"]["paths"])
                            for item in payload.get("files", []):
                                counts["batch_truncated_items"] += bool(item.get("truncated"))
                                if not item.get("truncated"):
                                    counts["checked_reads"] += 1
                                    name, content = item["path"], str(item.get("content"))
                                    counts["repeat_read_proxy"] += seen_reads.get(name) == content
                                    seen_reads[name] = content
                        failure = (
                            str(error.get("code"))
                            if error
                            else "nonzero_exit"
                            if isinstance(payload, dict)
                            and payload.get("exit_code") not in (0, None)
                            else None
                        )
                        signature = (
                            tool,
                            json.dumps(started["arguments"], sort_keys=True),
                            failure,
                        )
                        counts["repeated_failed_adjacent"] += (
                            failure is not None and prior_failed == signature
                        )
                        prior_failed = signature if failure else None
            run_rows.append(
                {
                    "run_id": row["run_id"],
                    "case_id": row["case_id"],
                    "success": result["success"],
                    "patches": result["patches"],
                    "format_rejections": rejected,
                    "terminal": result["status"],
                }
            )
            for path in [
                root / "result.json",
                root / "trace.jsonl",
                *sorted((root / "tokens").glob("turn_*.json")),
            ]:
                inputs[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        invalid = errors["unknown_tool"] + errors["invalid_arguments"]
        by_mode[mode] = {
            "counts": dict(counts),
            "tool_error_codes": dict(errors),
            "terminal_statuses": dict(terminals),
            "loop_failure_reasons": dict(reasons),
            "sample_upper_cny": str(spending),
            "upper_cny_per_success": str(spending / counts["successes"])
            if counts["successes"]
            else None,
            "format_rejection_rate": counts["format_rejections"] / counts["sampled_calls"]
            if counts["sampled_calls"]
            else None,
            "invalid_tool_call_rate": invalid / counts["tool_completions"]
            if counts["tool_completions"]
            else None,
            "repeat_read_proxy_rate": counts["repeat_read_proxy"] / counts["checked_reads"]
            if counts["checked_reads"]
            else None,
            "runs": run_rows,
        }
    return {
        "schema_version": 1,
        "comparison_key": key,
        "policies": by_mode,
        "input_sha256": inputs,
        "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limitations": [
            "repeat reads are a proxy; context visibility and semantic necessity unchecked",
            "public test failures are distinct from harness defects",
            "semantic tool-choice accuracy unchecked",
            "costs are no-cache rounded estimates, not invoices",
            "declared denominator is 16 per mode",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.output.open("x") as stream:
        json.dump(diagnose(args.directory), stream, indent=2)


if __name__ == "__main__":
    main()
