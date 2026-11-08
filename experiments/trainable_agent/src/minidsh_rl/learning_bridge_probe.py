"""Fixed-script real-container controls for the bridge, never policy-learning evidence."""

import argparse
import asyncio
import hashlib
import json
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from minidsh.domain import ModelRequest
from minidsh.infrastructure.containers import DockerHost
from minidsh_rl.learning_gates import PROJECT, _sources
from minidsh_rl.learning_rollout import PolicyBinding, assemble_group, run_learning_rollout
from minidsh_rl.learning_tasks import learning_cases
from minidsh_rl.minirepo_prompt_probe import ExampleBackend
from minidsh_rl.minirepo_verification import IMAGE
from minidsh_rl.model import Sample


def _patch(changes: list[dict[str, str]]) -> str:
    return (
        "<tool_call>"
        + json.dumps({"name": "apply_patch", "arguments": {"edits": changes}})
        + "</tool_call>"
    )


@dataclass(slots=True)
class _ContextOnly:
    requests: list[ModelRequest] = field(default_factory=list)
    calls: int = 0

    def render(self, request: ModelRequest) -> tuple[int, ...]:
        self.requests.append(request)
        return (1,) * 7000

    async def sample(self, prompt: tuple[int, ...], limit: int, seed: int) -> Sample:
        del prompt, limit, seed
        self.calls += 1
        raise AssertionError("context-only control must never enter sampler")


async def probe(output: Path, ledger: Path) -> None:
    """Four sequential deterministic controls with private criteria outside requests."""
    source = _sources()
    before = await asyncio.to_thread(ledger.read_bytes)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    with tarfile.open(output / "source.tar.gz", "w:gz") as archive:
        for name in source:
            archive.add(name, arcname=str(Path(name).relative_to(PROJECT)), recursive=False)
    (output / "source.json").write_text(json.dumps(source, indent=2))
    case = next(c for c in learning_cases() if c.task.family == "table_join" and c.variant == 0)
    edits = [{"path": f.module, "old": f.new, "new": f.old} for f in case.faults]
    test = '<tool_call>{"name":"run_tests","arguments":{}}</tool_call>'
    interactive = ExampleBackend([_patch(edits[:1]), test, _patch(edits[1:]), test, "Done."])
    single = ExampleBackend([_patch(edits), "Done."])
    unchanged = ExampleBackend(["No edit."])
    empty = _ContextOnly()
    policy = PolicyBinding(
        kind="scripted_control",
        policy_id="scripted_control",
        snapshot_sha256=hashlib.sha256(json.dumps(edits, sort_keys=True).encode()).hexdigest(),
        tokenizer_sha256=hashlib.sha256(b"synthetic_control_ids_not_a_model_tokenizer").hexdigest(),
    )
    one = await run_learning_rollout(
        case,
        interactive,
        policy=policy,
        run_id="bridge_interactive",
        seed=17,
        output=output / "interactive",
        host=DockerHost(),
        image=IMAGE,
    )
    two = await run_learning_rollout(
        case,
        single,
        policy=policy,
        run_id="bridge_single",
        seed=27,
        output=output / "single",
        host=DockerHost(),
        image=IMAGE,
        mode="single",
    )
    three = await run_learning_rollout(
        case,
        unchanged,
        policy=policy,
        run_id="bridge_unchanged",
        seed=37,
        output=output / "unchanged",
        host=DockerHost(),
        image=IMAGE,
    )
    four = await run_learning_rollout(
        case,
        empty,
        policy=policy,
        run_id="bridge_empty",
        seed=47,
        output=output / "empty",
        host=DockerHost(),
        image=IMAGE,
    )
    if not one.success or not two.success or three.success or four.success:
        raise ValueError("scripted candidate outcome mismatch")
    if (
        len(one.turns) != len(interactive.actions)
        or len(two.turns) != 1
        or four.turns
        or empty.calls
    ):
        raise ValueError("scripted/raw call accounting mismatch")
    operations = json.loads((output / "interactive/operations.json").read_text())
    codes = [p["exit_code"] for p in operations if p["operation"] == "test"]
    if codes != [1, 0]:
        raise ValueError("actual failed-feedback revision control did not occur")
    rejected = False
    try:
        assemble_group((one, two, three, four))
    except ValueError:
        rejected = True
    if not rejected:
        raise ValueError("scripted controls accepted for training")
    requests = [*interactive.requests, *single.requests, *unchanged.requests, *empty.requests]
    visible = "\n".join(m.content for r in requests for m in r.messages)
    if case.task.hidden_checks in visible:
        raise ValueError("hidden checker entered scripted requests")
    if _sources() != source or await asyncio.to_thread(ledger.read_bytes) != before:
        raise ValueError("source or ledger changed during control")
    result = {
        "schema_version": 1,
        "passed": True,
        "kind": "scripted_component_trajectory_control",
        "control_rollouts": 4,
        "new_provider_calls": 0,
        "new_training_calls": 0,
        "new_model_rollouts": 0,
        "model_queue_admitted": False,
        "partial_test_exit_codes": codes,
        "single_sampled_control_calls": len(two.turns),
        "single_synthetic_stop": json.loads((output / "single/result.json").read_text())[
            "synthetic_stop"
        ],
        "empty_control_calls": len(four.turns),
        "unchanged_candidate_success": three.success,
        "scripted_trajectories_rejected_for_training": rejected,
        "hidden_checker_absent_from_requests": True,
        "budget_ledger_unchanged": True,
        "budget_ledger_sha256": hashlib.sha256(before).hexdigest(),
        "dataset_version": case.dataset_version,
        "case_id": case.task.case_id,
        "source_sha256": source,
        "limitations": [
            "Synthetic token IDs/logprobs and scripted actions; not model performance.",
            "No provider snapshot authentication, real training or recovery execution tested.",
        ],
    }
    with (output / "summary.json").open("x") as stream:
        json.dump(result, stream, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--budget-ledger",
        type=Path,
        default=Path("benchmark-results/trainable_agent/expansion-v2/calibrated-budget.json"),
    )
    args = parser.parse_args()
    asyncio.run(probe(args.output, args.budget_ledger))


if __name__ == "__main__":
    main()
