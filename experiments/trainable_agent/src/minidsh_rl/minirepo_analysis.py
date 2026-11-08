"""Paired development diagnostic, trajectory audit and post-hoc patch validation."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from minidsh.infrastructure.containers import DockerHost
from minidsh_rl.minirepo_studies import BATCH_LIMIT, BATCH_READ, WORKFLOW, study_layout
from minidsh_rl.minirepo_tasks import development_tasks
from minidsh_rl.minirepo_verification import IMAGE, verify
from minidsh_rl.report import paired_interval, summarize

MIN_SIGNAL_PAIRS = 2
MIN_SIGNAL_FAMILIES = 2
DECLARED = 32


def feedback_revision_indices(
    operations: list[dict[str, object]], *, require_batch_before_feedback: bool = False
) -> tuple[int, ...]:
    """Indices of publicly failed candidates followed by a later successful patch."""
    current = 0
    failed: set[int] = set()
    revised: set[int] = set()
    batch_seen = False
    for operation in operations:
        count = int(str(operation["patches"]))
        if operation["operation"] == "patch":
            if count != current + 1:
                raise ValueError("inconsistent patch counter")
            current = count
            revised.update(index for index in failed if index < current)
        elif count != current:
            raise ValueError("inconsistent patch counter")
        if operation["operation"] == "read_many":
            batch_seen = True
        if (
            operation["operation"] == "test"
            and count > 0
            and operation.get("exit_code") not in (0, None)
            and (not require_batch_before_feedback or batch_seen)
        ):
            failed.add(count)
    return tuple(sorted(revised))


async def analyze(directory: Path, output: Path) -> None:  # noqa: PLR0912, PLR0915
    queue = json.loads(directory.joinpath("queue.json").read_text())
    protocol = json.loads(directory.joinpath("protocol.json").read_text())
    if await asyncio.to_thread(output.exists):
        raise FileExistsError("new analysis output required")
    if (
        len(queue) != protocol["declared"]
        or len(queue) != DECLARED
        or any(r["status"] != "completed" for r in queue)
    ):
        raise ValueError("complete predeclared denominator required for paired analysis")
    layout = study_layout(str(protocol.get("phase", WORKFLOW.phase)))
    prompt_study = layout.comparison_key == "prompt_variant"
    batch_study = layout in (BATCH_READ, BATCH_LIMIT)
    comparison_key = layout.comparison_key
    arms = layout.arms
    task_map = {t.case_id: t for t in development_tasks()}
    rows = []
    bindings = {}
    revision_cases = []
    batch_feedback_cases = []
    for row in queue:
        root = directory / row["run_id"]
        result = json.loads((root / "result.json").read_text())
        if any(result[k] != row[k] for k in ("case_id", "seed", "mode", "run_id")):
            raise ValueError("declared queue/result identity mismatch")
        if prompt_study and (
            result.get("prompt_variant") != row.get("prompt_variant")
            or result["mode"] != "interactive"
        ):
            raise ValueError("prompt arm identity mismatch")
        if batch_study and (
            result.get("read_variant") != row.get("read_variant")
            or result["mode"] != "interactive"
            or result.get("prompt_variant") != "original"
        ):
            raise ValueError("read arm identity mismatch")
        if layout == BATCH_LIMIT and result.get("batch_path_limit") != (
            8 if result["read_variant"] == "batch_read_eight" else 4
        ):
            raise ValueError("batch path limit identity mismatch")
        files = sorted((root / "tokens").glob("turn_*.json"))
        usage = generated = 0
        for i, path in enumerate(files):
            raw = json.loads(path.read_text())
            if (
                raw["call_index"] != i
                or raw["run_id"] != result["run_id"]
                or raw["seed"] != row["seed"] + i
                or raw["policy_id"] != result["policy_id"]
            ):
                raise ValueError("raw policy/run/seed mismatch")
            usage += len(raw["prompt_token_ids"]) + len(raw["completion_token_ids"])
            generated += len(raw["completion_token_ids"])
            if len(raw["completion_token_ids"]) != len(raw["old_logprobs"]):
                raise ValueError("missing sampled logprob")
        if (
            usage != result["total_tokens"]
            or generated != result["generated_tokens"]
            or len(files) != result["sampled_calls"]
        ):
            raise ValueError("token accounting mismatch")
        if result["mode"] == "single" and result["patches"] > 1:
            raise ValueError("single-submission contract violated")
        candidate_path = root / "candidate.json"
        if candidate_path.exists():
            candidate = json.loads(candidate_path.read_text())
            digest = hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()
            verdict = json.loads((root / "verification/result.json").read_text())
            if (
                digest != result["candidate_hash"]
                or verdict["candidate_sha256"] != digest
                or verdict["passed"] != result["success"]
            ):
                raise ValueError("private verdict/candidate binding mismatch")
        elif result["success"]:
            raise ValueError("success without candidate")
        records = dict(result)
        records["tokens"] = result["total_tokens"]
        rows.append(records)
        operations = json.loads((root / "operations.json").read_text())
        if batch_study and result.get("batch_read_calls") != sum(
            x["operation"] == "read_many" for x in operations
        ):
            raise ValueError("batch-read usage mismatch")
        # Verify the FIRST actual patch separately after the run, without revealing
        # any hidden verdict to the agent. This cannot change original rewards.
        if result["mode"] == "interactive" and result["patches"] > 1 and result["success"]:
            snapshots = json.loads((root / "patch-snapshots.json").read_text())
            task = task_map[row["case_id"]]
            first = {name: snapshots[0][name] for name in task.writable}
            logs = output.parent / (output.stem + "-first-patch-audit") / result["run_id"]
            verdict = await verify(DockerHost(), task, first, IMAGE, logs)
            revision_cases.append(
                {
                    "run_id": result["run_id"],
                    "case_id": result["case_id"],
                    "comparison_arm": result[comparison_key],
                    "first_patch_passed": verdict.passed,
                    "final_passed": True,
                    "failed_public_test_before_revision": any(
                        x["operation"] == "test"
                        and x["patches"] == 1
                        and x["exit_code"] not in (0, None)
                        for x in operations
                    ),
                }
            )
            if batch_study:
                early_batch = feedback_revision_indices(
                    operations, require_batch_before_feedback=True
                )
                for patch_index in feedback_revision_indices(operations):
                    candidate = {name: snapshots[patch_index - 1][name] for name in task.writable}
                    failed_logs = (
                        output.parent
                        / (output.stem + "-feedback-audit")
                        / result["run_id"]
                        / str(patch_index)
                    )
                    failed_verdict = await verify(DockerHost(), task, candidate, IMAGE, failed_logs)
                    batch_feedback_cases.append(
                        {
                            "run_id": result["run_id"],
                            "case_id": result["case_id"],
                            "comparison_arm": result[comparison_key],
                            "failed_patch_index": patch_index,
                            "pre_revision_private_passed": failed_verdict.passed,
                            "final_passed": True,
                            "used_batch_read": result["batch_read_calls"] > 0,
                            "batch_read_before_feedback": patch_index in early_batch,
                        }
                    )
        for path in [
            root / "result.json",
            root / "operations.json",
            root / "patch-snapshots.json",
            *files,
        ]:
            bindings[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    groups = {arm: [r for r in rows if r[comparison_key] == arm] for arm in arms}
    if any(len(group) != DECLARED // 2 for group in groups.values()):
        raise ValueError("unbalanced declared comparison arms")
    policies = {mode: summarize(selected, declared=16) for mode, selected in groups.items()}
    paired = []
    discordance = {f"{arm}_only": 0 for arm in arms}
    for i in range(0, len(queue), 2):
        selected = {r[comparison_key]: r for r in rows[i : i + 2]}
        if set(selected) != set(arms):
            raise ValueError("missing paired comparison arm")
        single, interactive = selected[arms[0]], selected[arms[1]]
        if (single["case_id"], single["seed"]) != (interactive["case_id"], interactive["seed"]):
            raise ValueError("unmatched paired case/seed")
        paired.append(
            (str(single["family"]), bool(single["success"]), bool(interactive["success"]))
        )
        discordance[f"{arms[0]}_only"] += bool(single["success"]) and not bool(
            interactive["success"]
        )
        discordance[f"{arms[1]}_only"] += bool(interactive["success"]) and not bool(
            single["success"]
        )
    interval = paired_interval(paired)
    interval["limitation"] = (
        "four development families; exploratory comparison, not RL efficacy or equivalence"
    )
    unique_families = {
        task_map[r["case_id"]].family
        for r in revision_cases
        if r["comparison_arm"] == arms[1]
        and not r["first_patch_passed"]
        and r["failed_public_test_before_revision"]
    }
    if batch_study:
        unique_families = {
            task_map[r["case_id"]].family
            for r in batch_feedback_cases
            if r["comparison_arm"] == arms[1]
            and r["batch_read_before_feedback"]
            and not r["pre_revision_private_passed"]
        }
    signal = (
        float(str(interval["difference"])) > 0
        and discordance[f"{arms[1]}_only"] >= MIN_SIGNAL_PAIRS
        and len(unique_families) >= MIN_SIGNAL_FAMILIES
    )
    report = {
        "schema_version": 1,
        "phase": layout.phase,
        "comparison_key": comparison_key,
        "arms": arms,
        "declared": 32,
        "completed": 32,
        "policies": policies,
        "paired": interval,
        "discordance": discordance,
        "revision_cases": revision_cases,
        "batch_feedback_revision_cases": batch_feedback_cases,
        "feedback_revision_families": sorted(unique_families),
        "interaction_signal_gate": signal,
        "training_automatically_authorized": False,
        "limitations": [
            "one model; four development families; no new held-out RL comparison",
            "batch-limit comparison changes path-list cap and its schema/description"
            if layout == BATCH_LIMIT
            else "batch comparison changes optional read capability and tool-schema context"
            if batch_study
            else "prompt comparison changes example text and context length"
            if prompt_study
            else "workflow comparison changes post-submission feedback and revision opportunity",
            (
                "verified candidate failure then final success is a trajectory proxy, "
                "not proof of causal reasoning"
            ),
        ],
        "input_sha256": bindings,
        "scorer_sha256": hashlib.sha256(
            await asyncio.to_thread(Path(__file__).read_bytes)
        ).hexdigest(),
    }
    with output.open("x") as stream:
        json.dump(report, stream, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(analyze(args.directory, args.output))


if __name__ == "__main__":
    main()
