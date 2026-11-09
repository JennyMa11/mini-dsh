"""Prepare one new v4 execution while retaining the failed initialization and hold."""

import argparse
import asyncio
import hashlib
import json
import sys
import tarfile
from pathlib import Path

from minidsh.infrastructure.locks import FileLease
from minidsh_rl.curve_cases_v4 import curve_cases_v4
from minidsh_rl.curve_readiness import verify_readiness
from minidsh_rl.curve_recovery import FencedResources, freeze_failed_restore, freeze_failure
from minidsh_rl.curve_recovery_remote import terminal_metadata
from minidsh_rl.curve_run import validate_config, validate_data
from minidsh_rl.curve_schedule import CurvePlan, make_curve_plan
from minidsh_rl.empty_optimizer import empty_optimizer
from minidsh_rl.learning_curve_budget import LearningCurveBudget
from minidsh_rl.learning_gates import PROJECT, _sources
from minidsh_rl.learning_run import ExecutionConfig
from minidsh_rl.learning_schedule import atomic_record, digest

# ruff: noqa: ASYNC240 - bounded local freeze has no concurrent asynchronous work


async def prepare_recovery(  # noqa: PLR0912, PLR0915 - explicit immutable preparation stages
    old: Path, root: Path, inspection: Path, verified_fresh: Path | None = None
) -> CurvePlan:
    if (old / "recovery-claim.json").exists():
        raise ValueError("initialization recovery already claimed")
    previous = CurvePlan.model_validate_json((old / "plan.json").read_bytes())
    expected = make_curve_plan(dataset="curve_dataset_v4")
    if (
        previous.rows != expected.rows
        or previous.case_sha256 != expected.case_sha256
        or previous.required_cny != expected.required_cny
        or (previous.recovery_sha256 is not None and verified_fresh is None)
        or previous.initialization != "restore_optimizer"
        or previous.finance_directory is None
        or previous.budget_baseline is None
    ):
        raise ValueError("original frozen science or financial binding changed")
    for name, sha in previous.initial_inputs.items():
        if digest(old / name) != sha:
            raise ValueError("original initial inputs changed")
    with tarfile.open(old / "source.tar.gz") as archive:
        for name, sha in previous.sources.items():
            stream = archive.extractfile(str(Path(name).relative_to(PROJECT)))
            if stream is None or hashlib.sha256(stream.read()).hexdigest() != sha:
                raise ValueError("original source archive changed")
    config = ExecutionConfig.model_validate_json((old / "execution-config.json").read_bytes())
    validate_config(config)
    validate_data(old / "data-summary.json", curve_cases_v4())
    verify_readiness(old / "external-readiness.json")
    if verified_fresh is None:
        record = json.loads(inspection.read_text())
        if record["matches"] != 1:
            raise ValueError("unique owned failed run required")
        uid = record["matched_runs"][0]["training_run_cuid"]
    else:
        empty_optimizer(verified_fresh)
        uid = json.loads((old / "initial-sdk/registered-client.json").read_text())[
            "training_run_cuid"
        ]
    source = json.loads((old / "source-checkpoint.json").read_text())["optimizer_uri"]
    metadata = await terminal_metadata(uid, source)
    root.mkdir(parents=True, exist_ok=False)
    (root / "terminal-metadata.json").write_text(json.dumps(metadata, indent=2))
    freezer = freeze_failure if verified_fresh is None else freeze_failed_restore
    proof = freezer(old, root / "terminal-metadata.json", root / "recovery-evidence.json")
    with LearningCurveBudget(Path(previous.finance_directory), proof.original_baseline) as budget:
        if (
            FencedResources(budget, root / "recovery-evidence.json").has_pending
            or budget.available < expected.required_cny
        ):
            raise ValueError("complete queue or initialization recovery not admitted")
        snapshot = budget.path.read_bytes()
    for name in (
        "source-checkpoint.json",
        "source-pair.json",
        "initial-source.json",
        "data-summary.json",
        "execution-config.json",
        "external-readiness.json",
    ):
        (root / name).write_bytes((old / name).read_bytes())
    (root / "budget-before.json").write_bytes(snapshot)
    initial_inputs = {name: digest(root / name) for name in previous.initial_inputs}
    if verified_fresh is not None:
        (root / "source-optimizer.archive").write_bytes(verified_fresh.read_bytes())
        initial_inputs["source-optimizer.archive"] = digest(root / "source-optimizer.archive")
    (root / "source-inputs.json").write_text(json.dumps(initial_inputs))
    plan = previous.model_copy(
        update={
            "sources": _sources(),
            "initial_inputs": initial_inputs,
            "recovery_sha256": digest(root / "recovery-evidence.json"),
            "initialization": "restore_optimizer"
            if verified_fresh is None
            else "verified_fresh_seed17",
            "source_optimizer_archive_sha256": None
            if verified_fresh is None
            else digest(verified_fresh),
        }
    )
    atomic_record(root / "plan.json", plan, create=True)
    with tarfile.open(root / "source.tar.gz", "w:gz") as archive:
        for name in plan.sources:
            archive.add(name, arcname=str(Path(name).relative_to(PROJECT)), recursive=False)
    lease = FileLease(old / "controller.lock")
    try:
        with (old / "recovery-claim.json").open("x") as file:
            json.dump(
                {"recovery_root": str(root.resolve()), "evidence_sha256": plan.recovery_sha256},
                file,
            )
    finally:
        lease.close()
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--inspection", type=Path, required=True)
    parser.add_argument("--verified-fresh", type=Path)
    args = parser.parse_args()
    try:
        plan = asyncio.run(
            prepare_recovery(args.source, args.directory, args.inspection, args.verified_fresh)
        )
        sys.stdout.write(
            json.dumps(
                {
                    "declared": len(plan.rows),
                    "required_cny": str(plan.required_cny),
                    "original_baseline": str(plan.budget_baseline),
                    "old_hold_preserved": True,
                }
            )
        )
    except Exception as error:
        sys.stdout.write(
            json.dumps({"error_type": type(error).__name__, "automatic_retry": False}) + "\n"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
