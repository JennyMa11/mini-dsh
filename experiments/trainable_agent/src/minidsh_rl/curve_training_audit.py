"""Independent offline reconstruction of each attempted G4 and normalized PPO datum."""

import argparse
import json
import math
from pathlib import Path

from minidsh_rl.curve_artifacts import verify_hashes
from minidsh_rl.curve_execution import CurveState
from minidsh_rl.curve_schedule import GROUP_SIZE, CurvePlan, curve_cases_for
from minidsh_rl.learning_execution import GroupRecord, UpdateReceipt, group_datum_hash
from minidsh_rl.learning_rollout import assemble_group, load_rollout
from minidsh_rl.learning_schedule import digest
from minidsh_rl.trainer import normalized_batch, policy_diagnostics
from minidsh_rl.transaction import UpdateJournal


def audit_training(root: Path, output: Path) -> dict[str, object]:  # noqa: PLR0912 - independent ordered evidence checks
    plan = CurvePlan.model_validate_json((root / "plan.json").read_bytes())
    state = CurveState.model_validate_json((root / "curve-state.json").read_bytes())
    if state.kind != "provider" or state.plan_sha256 != digest(root / "plan.json"):
        raise ValueError("real provider provenance required")
    verify_hashes(root, state.artifacts)
    cases = {case.task.case_id: case for case in curve_cases_for(plan.dataset_version)}
    records: list[dict[str, object]] = []
    confirmed = 0
    for folder in sorted((root / "updates").glob("train_*")):
        prepared = GroupRecord.model_validate_json((folder / "group.json").read_bytes())
        rows = [row for row in plan.rows if row.phase == "train" and row.group == folder.name]
        if len(rows) != GROUP_SIZE or tuple(row.run_id for row in rows) != prepared.run_ids:
            raise ValueError("training group does not match frozen queue")
        verify_hashes(root, prepared.input_sha256)
        values = tuple(
            load_rollout(cases[row.case_id], root / "rollouts" / row.run_id, prepared.before.policy)
            for row in rows
        )
        if any(
            value.run_id != row.run_id or value.seed != row.seed
            for row, value in zip(rows, values, strict=True)
        ):
            raise ValueError("training trajectory identity changed")
        group = assemble_group(values)
        datum_hash = group_datum_hash(group)
        if datum_hash != prepared.datum_sha256 or prepared.has_signal != bool(
            group.batch and group.batch.has_signal
        ):
            raise ValueError("reconstructed normalized datum/signal mismatch")
        slot = int(folder.name.rsplit("_", 1)[-1])
        path = folder / f"update_{slot:03}.json"
        journal = UpdateJournal.load(path) if path.exists() else None
        if journal is not None and journal.datum_hash != datum_hash:
            raise ValueError("journal datum hash mismatch")
        if journal is not None and journal.state in {"optim_confirmed", "checkpointed"}:
            confirmed += 1
        policy_path = folder / f"policy_{slot:03}.json"
        if policy_path.exists():
            if group.batch is None:
                raise ValueError("forward diagnostics without batch")
            raw = json.loads((folder / f"forward_{slot:03}.json").read_text())
            calculated = policy_diagnostics(
                normalized_batch(group.batch, "sample_mean"),
                tuple(
                    tuple(float(x) for x in item["logprobs"]["data"])
                    for item in raw["loss_fn_outputs"]
                ),
            )
            recorded = json.loads(policy_path.read_text())
            if set(calculated) != set(recorded) or any(
                not math.isclose(calculated[key], recorded[key], rel_tol=1e-10, abs_tol=1e-12)
                for key in calculated
            ):
                raise ValueError("fresh-policy numerical diagnostics mismatch")
        receipt_path = folder / "receipt.json"
        if receipt_path.exists():
            receipt = UpdateReceipt.model_validate_json(receipt_path.read_bytes())
            if (
                receipt.group_sha256 != digest(folder / "group.json")
                or journal is None
                or receipt.journal_sha256 != digest(path)
                or receipt.before != prepared.before
                or receipt.updated != prepared.has_signal
                or not journal.safe_to_resume
                or (receipt.updated and journal.checkpoint != receipt.after.optimizer_uri)
            ):
                raise ValueError("confirmed training receipt changed")
        elif folder.name in state.groups:
            raise ValueError("committed group is missing its confirmed receipt")
        records.append(
            {
                "group": folder.name,
                "run_ids": group.run_ids,
                "rewards": group.rewards,
                "advantages": group.advantages,
                "normalized_datum_sha256": datum_hash,
                "assistant_tokens": group.batch.assistant_tokens if group.batch else 0,
                "compute_tokens": group.batch.compute_tokens if group.batch else 0,
                "padded_tokens": group.batch.padded_tokens if group.batch else 0,
                "has_signal": prepared.has_signal,
                "journal_state": journal.state if journal else None,
                "receipt_confirmed": receipt_path.exists(),
                "controller_committed": folder.name in state.groups,
            }
        )
    result: dict[str, object] = {
        "schema_version": 1,
        "declared_groups": 24,
        "attempted_groups": len(records),
        "controller_committed_groups": len(state.groups),
        "confirmed_optimizer_updates": confirmed,
        "unknown_optimizer_submissions": sum(
            row["journal_state"] == "optim_submitted" for row in records
        ),
        "source_plan_sha256": digest(root / "plan.json"),
        "generator_sha256": digest(Path(__file__)),
        "new_provider_calls": 0,
        "new_training_calls": 0,
        "groups": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(result, stream, indent=2)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit_training(args.directory, args.output)


if __name__ == "__main__":
    main()
