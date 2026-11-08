"""Bind training rewards to independent verdicts and each sampled policy/seed."""

import argparse
import hashlib
import json
from pathlib import Path

from minidsh.evaluation.tracefile import TraceRun
from minidsh_rl.analyze_v2 import audit_training
from minidsh_rl.contracts import Turn
from minidsh_rl.train_v2 import training_schedule

MAX_CALLS = 8
MAX_TOTAL = 24000
MAX_GENERATED = 2048
MAX_CONTEXT = 4096


def sampled_turns(directory: Path, *, run_id: str, policy: str, seed: int) -> tuple[Turn, ...]:
    """Metadata, exact requested sampling settings and Token contracts are immutable."""
    result = []
    for index, path in enumerate(sorted((directory / "tokens").glob("turn_*.json"))):
        raw = json.loads(path.read_text())
        if (
            raw["run_id"] != run_id
            or raw["policy_id"] != policy
            or raw["call_index"] != index
            or raw["seed"] != seed + index
            or raw["temperature"] != 1.0
            or raw["top_p"] != 1.0
        ):
            raise ValueError("sample policy/seed/settings mismatch")
        result.append(
            Turn(
                tuple(raw["prompt_token_ids"]),
                tuple(raw["completion_token_ids"]),
                tuple(raw["old_logprobs"]),
            )
        )
    if (
        len(result) > MAX_CALLS
        or sum(len(t.prompt) + len(t.completion) for t in result) > MAX_TOTAL
        or sum(len(t.completion) for t in result) > MAX_GENERATED
        or any(len(t.prompt) + len(t.completion) > MAX_CONTEXT for t in result)
    ):
        raise ValueError("sampled Token envelope exceeded")
    return tuple(result)


def audit(directory: Path) -> dict[str, object]:  # noqa: PLR0912, PLR0915 - complete trajectory binding
    numerical = audit_training(directory)
    rows = json.loads((directory / "queue.json").read_text())
    schedule = training_schedule("pilot")
    if len(rows) != len(schedule):
        raise ValueError("declared Pilot schedule changed")
    policy = directory.name + "_policy_0"
    verified = successes = known_local = model_calls = 0
    hashes = {}
    bindings = []
    for slot, (row, task) in enumerate(zip(rows, schedule, strict=True)):
        if row["slot"] != slot or row["case_id"] != task.case_id or row["status"] != "completed":
            raise ValueError("training slot/case/order mismatch")
        group = json.loads((directory / f"group_{slot:03}.json").read_text())
        if group["policy_id"] != policy:
            raise ValueError("group snapshot progression mismatch")
        expected_ids = [f"{directory.name}_{slot:03}_{repeat}" for repeat in range(4)]
        if group["run_ids"] != expected_ids:
            raise ValueError("group rollout identity changed")
        for repeat, run_id in enumerate(expected_ids):
            run = directory / run_id
            result = json.loads((run / "result.json").read_text())
            seed = 17 + slot * 100 + repeat
            if (
                result["case_id"] != task.case_id
                or result["run_id"] != run_id
                or result["policy_id"] != policy
                or result["seed"] != seed
                or result["split"] != "train"
            ):
                raise ValueError("result/task/policy mismatch")
            raw = sampled_turns(run, run_id=run_id, policy=policy, seed=seed)
            trace = TraceRun.from_file(run / "trace.jsonl", run_id=run_id)
            i, o = sum(len(t.prompt) for t in raw), sum(len(t.completion) for t in raw)
            if (
                trace.status() != result["status"]
                or trace.final() is None
                or trace.total_tokens()[:2] != (i, o)
                or i + o != result["total_tokens"]
                or len(trace.by_name("model_call_completed")) != len(raw)
            ):
                raise ValueError("training Trace/raw/result mismatch")
            failures = trace.by_name("model_call_failed")
            for record in failures:
                if (
                    record.data.get("error")
                    != "context_overflow: experiment token budget exhausted"
                ):
                    raise ValueError("unknown provider failure in training cohort")
                known_local += 1
            model_calls += len(raw)
            if result["candidate_hash"]:
                candidate = run / "candidate.py"
                if hashlib.sha256(candidate.read_bytes()).hexdigest() != result["candidate_hash"]:
                    raise ValueError("training candidate changed")
                verdict = json.loads((run / "verification/result.json").read_text())
                if (
                    verdict["case_id"] != task.case_id
                    or verdict["passed"] != result["success"]
                    or verdict["criterion_version"] != "expansion_v2"
                ):
                    raise ValueError("training reward/independent verdict mismatch")
                verified += 1
                verdict_path = run / "verification/result.json"
                bindings.append(
                    {
                        "run_id": run_id,
                        "case_id": task.case_id,
                        "criterion_version": "expansion_v2",
                        "passed": verdict["passed"],
                        "candidate_sha256": result["candidate_hash"],
                        "source": str(verdict_path),
                        "source_sha256": hashlib.sha256(verdict_path.read_bytes()).hexdigest(),
                    }
                )
            elif result["success"] is not False:
                raise ValueError("training success without verified candidate")
            if group["rewards"][repeat] != float(result["success"]):
                raise ValueError("group reward differs from independently bound result")
            successes += result["success"] is True
            for path in run.rglob("*"):
                if path.is_file() and path.suffix in {".json", ".jsonl", ".py"}:
                    hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        if row["updated"]:
            policy = f"{directory.name}_policy_{slot + 1:03}"
    return {
        "schema_version": 1,
        "passed": True,
        "training_audit": numerical,
        "declared_rollouts": len(rows) * 4,
        "audited_rollouts": len(rows) * 4,
        "captured_candidate_verdicts": verified,
        "successes": successes,
        "model_calls": model_calls,
        "known_local_pre_rpc_failures": known_local,
        "unknown_provider_failures": 0,
        "input_hashes": hashes,
        "criterion_bindings": bindings,
        "training_summary_sha256": hashlib.sha256(
            (directory / "summary.json").read_bytes()
        ).hexdigest(),
        "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.training)
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
