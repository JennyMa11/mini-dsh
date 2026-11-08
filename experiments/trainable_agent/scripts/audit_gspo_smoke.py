"""Read-only post-hoc GSPO audit; no model, optimizer or network calls."""

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import torch
from minidsh_rl.contracts import Turn, advantages
from minidsh_rl.gspo import GSPOConfig, build_gspo_batch, make_loss


def audit(directory: Path) -> dict[str, object]:  # noqa: PLR0912, PLR0915 - independent evidence checks
    """Rebuild the mathematical objective from exact sampled arrays and verdicts."""
    inputs: dict[str, str] = {}

    def read(path: Path) -> object:
        inputs[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return json.loads(path.read_text())

    # JSON sidecars are private artifacts produced by the frozen controller.
    # Keep validation explicit; none of their content is passed to SwanLab.
    protocol = json.loads((directory / "protocol.json").read_text())
    sources = json.loads((directory / "source.json").read_text())
    for module in ("gspo.py", "datum.py", "contracts.py"):
        path = Path("experiments/trainable_agent/src/minidsh_rl") / module
        if hashlib.sha256(path.read_bytes()).hexdigest() != sources[str(path)]:
            raise ValueError("loss/contract source changed; use the archived source for replay")
    config = GSPOConfig(**protocol["config"])
    trajectories = tokens = tools = compute = assistant = 0
    successes = 0
    statuses: Counter[str] = Counter()
    groups: list[dict[str, object]] = []
    for slot in range(protocol["groups"]):
        group_path = directory / f"group_{slot:03}.json"
        read(group_path)
        group = json.loads(group_path.read_text())
        turns = []
        rewards = []
        for repeat, run_id in enumerate(group["run_ids"]):
            root = directory / run_id
            result_path = root / "result.json"
            read(result_path)
            result = json.loads(result_path.read_text())
            candidate = root / "candidate.py"
            if result["candidate_hash"] is not None:
                inputs[str(candidate)] = hashlib.sha256(candidate.read_bytes()).hexdigest()
                if inputs[str(candidate)] != result["candidate_hash"]:
                    raise ValueError("candidate binding mismatch")
                verdict_path = root / "verification/result.json"
                read(verdict_path)
                verdict = json.loads(verdict_path.read_text())
                if bool(verdict["passed"]) != result["success"]:
                    raise ValueError("private verdict mismatch")
            elif result["success"]:
                raise ValueError("success without verified candidate")
            recorded = []
            for index, path in enumerate(sorted((root / "tokens").glob("turn_*.json"))):
                read(path)
                raw = json.loads(path.read_text())
                if (
                    raw["run_id"] != run_id
                    or raw["policy_id"] != group["policy_id"]
                    or raw["call_index"] != index
                    or raw["seed"] != 3000 + slot * 100 + repeat + index
                ):
                    raise ValueError("recorded policy/run/seed mismatch")
                recorded.append(
                    Turn(
                        tuple(raw["prompt_token_ids"]),
                        tuple(raw["completion_token_ids"]),
                        tuple(raw["old_logprobs"]),
                    )
                )
            turns.append(tuple(recorded))
            rewards.append(float(result["success"]))
            trajectories += 1
            successes += result["success"]
            tokens += result["total_tokens"]
            tools += result["tool_calls"]
            statuses[result["status"]] += 1
        relative = advantages(tuple(rewards))
        if list(relative) != group["advantages"] or rewards != group["rewards"]:
            raise ValueError("group reward/advantage mismatch")
        batch = build_gspo_batch(tuple(turns), relative)
        serialized = json.dumps(
            {
                "datums": [d.model_dump(mode="json") for d in batch.datums],
                "trajectories": [asdict(t) for t in batch.trajectories],
                "config": asdict(config),
            },
            sort_keys=True,
        )
        journal_path = directory / f"update_{slot:03}.json"
        read(journal_path)
        journal = json.loads(journal_path.read_text())
        if journal["datum_hash"] != hashlib.sha256(serialized.encode()).hexdigest():
            raise ValueError("GSPO batch hash mismatch")
        if batch.has_signal:
            if journal["state"] != "checkpointed":
                raise ValueError("unknown training update")
            values_path = directory / f"current_logprobs_{slot:03}.json"
            read(values_path)
            values = [
                torch.tensor(x, requires_grad=True) for x in json.loads(values_path.read_text())
            ]
            loss, metrics = make_loss(batch, config)(list(batch.datums), values)
            grads = torch.autograd.grad(loss, values)
            flat = [turn for trajectory in turns for turn in trajectory]
            if any(
                bool((g.flatten()[: len(t.prompt) - 1] != 0).any())
                for t, g in zip(flat, grads, strict=True)
            ):
                raise ValueError("nonzero direct prompt/observation loss gradient")
            compute += batch.padded_tokens * 2
            assistant += batch.assistant_tokens
            groups.append(
                {
                    "slot": slot,
                    "rewards": rewards,
                    "datums": len(batch.datums),
                    "mask_passed": True,
                    "metrics": metrics,
                }
            )
        elif journal["state"] != "skipped":
            raise ValueError("zero-signal group not skipped")
    for name in ("protocol.json", "source.json", "summary.json"):
        read(directory / name)
    return {
        "schema_version": 1,
        "passed": True,
        "declared": protocol["groups"] * protocol["group_size"],
        "completed": trajectories,
        "training_successes": successes,
        "inference_tokens": tokens,
        "tool_calls": tools,
        "double_pass_padded_upper_tokens": compute,
        "assistant_loss_tokens": assistant,
        "terminal_statuses": dict(statuses),
        "groups": groups,
        "input_sha256": inputs,
        "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "engineering smoke, not held-out evaluation; direct target loss mask only",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.directory)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)


if __name__ == "__main__":
    main()
