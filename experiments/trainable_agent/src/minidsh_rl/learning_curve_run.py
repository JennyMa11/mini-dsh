"""Fresh v4 train/validation curve with immutable accepted development provenance."""

import argparse
import asyncio
import json
import logging
import sys
import tarfile
import traceback
from decimal import Decimal
from pathlib import Path

import pytrio as trio  # type: ignore[import-untyped]

from minidsh.infrastructure.process import CommandPolicy, LocalCommandRunner
from minidsh.infrastructure.sandbox import WorkspaceSandbox
from minidsh_rl.billing_calibration import price_card
from minidsh_rl.cost_store import _restore, _State
from minidsh_rl.curve_budget import CurveBudget
from minidsh_rl.curve_cases_v4 import curve_cases_v4
from minidsh_rl.curve_execution import CurveState, execute_curve, remaining_allocation
from minidsh_rl.curve_readiness import freeze_readiness, verify_readiness
from minidsh_rl.curve_recovery import resources_for, validate_failure
from minidsh_rl.curve_recovery_remote import terminal_metadata
from minidsh_rl.curve_run import validate_config, validate_data
from minidsh_rl.curve_schedule import CurvePlan, make_curve_plan
from minidsh_rl.curve_sdk import final_audit
from minidsh_rl.development_run import owned_metadata
from minidsh_rl.development_schedule import DevelopmentPlan
from minidsh_rl.empty_optimizer import empty_optimizer
from minidsh_rl.learning_curve_budget import CAP, LearningCurveBudget
from minidsh_rl.learning_curve_runtime import LearningCurveRuntime
from minidsh_rl.learning_execution import Checkpoint
from minidsh_rl.learning_gates import PROJECT, _sources, audit
from minidsh_rl.learning_run import ExecutionConfig
from minidsh_rl.learning_schedule import atomic_record, digest
from minidsh_rl.learning_sdk import LeasedResources, SavedPair, _binding
from minidsh_rl.preflight import service_evidence
from minidsh_rl.rpc_diagnostics import rpc_diagnostic


def prepare_learning_curve(root: Path, data: Path, development: Path, finance: Path) -> CurvePlan:
    validate_data(data, curve_cases_v4())
    config = ExecutionConfig.model_validate_json(
        (development / "execution-config.json").read_bytes()
    )
    validate_config(config)
    old_plan = DevelopmentPlan.model_validate_json((development / "plan.json").read_bytes())
    archives = [Path(p) for p in old_plan.inputs if p.endswith("/initial-sdk/initial.archive")]
    if len(archives) != 1:
        raise ValueError("unique admitted initial archive required")
    source = Checkpoint.model_validate_json((development / "source-checkpoint.json").read_bytes())
    pair = SavedPair.model_validate_json((development / "source-pair.json").read_bytes())
    if (
        _binding(pair) != source.policy
        or pair.optimizer.path != source.optimizer_uri
        or pair.sampler.path != source.sampler_uri
    ):
        raise ValueError("admitted optimizer/sampler identity changed")
    expected = make_curve_plan(dataset="curve_dataset_v4")
    if expected.required_cny > CAP:
        raise ValueError("complete allocation exceeds study ceiling")
    with CurveBudget(finance) as budget:
        if LeasedResources(budget).has_pending or budget.available < expected.required_cny:
            raise ValueError("complete curve budget unavailable")
        baseline = budget.committed
        snapshot = budget.path.read_bytes()
    root.mkdir(parents=True, exist_ok=False)
    freeze_readiness(development, root / "external-readiness.json")
    (root / "data-summary.json").write_bytes(data.read_bytes())
    atomic_record(root / "execution-config.json", config, create=True)
    atomic_record(root / "source-checkpoint.json", source, create=True)
    atomic_record(root / "source-pair.json", pair, create=True)
    (root / "initial-source.json").write_text(
        json.dumps({"archive": str(archives[0]), "sha256": digest(archives[0])})
    )
    (root / "budget-before.json").write_bytes(snapshot)
    initial_inputs = {
        name: digest(root / name)
        for name in (
            "source-checkpoint.json",
            "source-pair.json",
            "initial-source.json",
            "budget-before.json",
        )
    }
    plan = expected.model_copy(
        update={
            "sources": _sources(),
            "data_summary_sha256": digest(root / "data-summary.json"),
            "config_sha256": digest(root / "execution-config.json"),
            "readiness_sha256": digest(root / "external-readiness.json"),
            "finance_directory": str(finance.resolve()),
            "budget_baseline": baseline,
            "initial_inputs": initial_inputs,
        }
    )
    atomic_record(root / "plan.json", plan, create=True)
    (root / "source-inputs.json").write_text(
        json.dumps(
            {
                name: digest(root / name)
                for name in (
                    "source-checkpoint.json",
                    "source-pair.json",
                    "initial-source.json",
                    "budget-before.json",
                )
            }
        )
    )
    with tarfile.open(root / "source.tar.gz", "w:gz") as archive:
        for name in plan.sources:
            archive.add(name, arcname=str(Path(name).relative_to(PROJECT)), recursive=False)
    return plan


def load_learning_curve(root: Path) -> tuple[CurvePlan, ExecutionConfig]:
    plan = CurvePlan.model_validate_json((root / "plan.json").read_bytes())
    expected = make_curve_plan(dataset="curve_dataset_v4")
    if (
        plan.dataset_version != expected.dataset_version
        or plan.rows != expected.rows
        or plan.case_sha256 != expected.case_sha256
        or plan.required_cny != expected.required_cny
        or plan.sources != _sources()
        or plan.config_sha256 != digest(root / "execution-config.json")
        or plan.data_summary_sha256 != digest(root / "data-summary.json")
        or plan.readiness_sha256 != digest(root / "external-readiness.json")
        or plan.finance_directory is None
        or plan.budget_baseline is None
    ):
        raise ValueError("frozen v4 source/config/data changed")
    inputs = json.loads((root / "source-inputs.json").read_text())
    if plan.initialization == "verified_fresh_seed17":
        archive = root / "source-optimizer.archive"
        if plan.source_optimizer_archive_sha256 != digest(archive) or plan.recovery_sha256 is None:
            raise ValueError("verified fresh source optimizer missing or changed")
        empty_optimizer(archive)
    if inputs != plan.initial_inputs:
        raise ValueError("initial source manifest changed")
    if any(digest(root / name) != expected_hash for name, expected_hash in inputs.items()):
        raise ValueError("initial source input changed")
    source = json.loads((root / "initial-source.json").read_text())
    if digest(Path(source["archive"])) != source["sha256"]:
        raise ValueError("initial source archive changed")
    verify_readiness(root / "external-readiness.json")
    if plan.recovery_sha256 is not None:
        proof_path = root / "recovery-evidence.json"
        if digest(proof_path) != plan.recovery_sha256:
            raise ValueError("frozen recovery evidence changed")
        proof = validate_failure(proof_path)
        checkpoint = Checkpoint.model_validate_json((root / "source-checkpoint.json").read_bytes())
        if checkpoint.optimizer_uri != proof.source_optimizer:
            raise ValueError("recovery original optimizer changed")
    validate_data(root / "data-summary.json", curve_cases_v4())
    config = ExecutionConfig.model_validate_json((root / "execution-config.json").read_bytes())
    validate_config(config)
    return plan, config


async def run_learning_curve(root: Path, *, resume: bool = False) -> CurveState:
    plan, config = load_learning_curve(root)
    if plan.finance_directory is None or plan.budget_baseline is None:
        raise ValueError("financial binding missing")
    path = root / "curve-state.json"
    old = CurveState.model_validate_json(path.read_bytes()) if path.exists() else None
    if old is not None and old.phase != "running":
        return old
    if old is not None and not resume:
        raise ValueError("explicit confirmed resume required")
    with LearningCurveBudget(Path(plan.finance_directory), plan.budget_baseline) as budget:
        resources = resources_for(root, plan, budget)
        need = plan.required_cny if old is None else remaining_allocation(plan, old)
        if resources.has_pending or budget.available < need:
            raise ValueError("complete remaining curve allocation unavailable")
        before = _restore(_State.model_validate_json((root / "budget-before.json").read_bytes()))
        if old is None and budget.committed != before.committed:
            raise ValueError("financial baseline changed before execution")
        if plan.recovery_sha256 is not None:
            proof = validate_failure(root / "recovery-evidence.json")
            metadata = await terminal_metadata(proof.training_run_cuid, proof.source_optimizer)
            with (root / "terminal-before-run.json").open("x") as output:
                json.dump(metadata, output, indent=2)
        attempt = len(list((root / "live-preflight").glob("*.json"))) + 1
        live = root / "live-preflight" / f"attempt_{attempt:03}.json"
        prefix = "curve4_" + digest(root / "plan.json")[:16] + f"_preflight_{attempt}"
        budget.reserve(prefix, Decimal(".05"))
        await service_evidence(live)
        price_card(live)
        pair = SavedPair.model_validate_json((root / "source-pair.json").read_bytes())
        metadata = await owned_metadata(pair)
        (root / f"owned-source-{attempt:03}.json").write_text(json.dumps(metadata, indent=2))
        budget.settle(prefix, Decimal(".05"))
        load_learning_curve(root)
        host = LocalCommandRunner(WorkspaceSandbox(root), CommandPolicy(frozenset({"docker"})))
        runtime = LearningCurveRuntime(
            trio.ServiceClient(), budget, root, host, config.tokenizer_sha256, image=config.image
        )
        try:
            state = await execute_curve(root, plan, resources, runtime, resume=resume)
            if state.phase == "finished" and state.updated and state.current is not None:
                await final_audit(runtime, state.current)
            return state
        finally:
            (root / "budget-after.json").write_bytes(budget.path.read_bytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit", "prepare", "inspect", "run"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--finance", type=Path, required=True)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--development", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    for name in ("httpx", "httpcore", "pytrio"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        if args.command == "audit":
            asyncio.run(
                audit(
                    curve_cases_v4(),
                    args.directory,
                    args.finance / "budget.json",
                    policy="curve_pure_v1",
                )
            )
        elif args.command == "prepare":
            if args.data is None or args.development is None:
                parser.error("prepare requires --data and --development")
            plan = prepare_learning_curve(args.directory, args.data, args.development, args.finance)
            sys.stdout.write(
                json.dumps(
                    {
                        "rows": len(plan.rows),
                        "required_cny": str(plan.required_cny),
                        "cap_cny": str(CAP),
                    }
                )
                + "\n"
            )
        elif args.command == "inspect":
            plan, _ = load_learning_curve(args.directory)
            if plan.budget_baseline is None or plan.finance_directory is None:
                raise ValueError("missing financial binding")
            with LearningCurveBudget(Path(plan.finance_directory), plan.budget_baseline) as budget:
                sys.stdout.write(
                    json.dumps(
                        {
                            "available_cny": str(budget.available),
                            "required_cny": str(plan.required_cny),
                            "pending": resources_for(args.directory, plan, budget).has_pending,
                            "held_pending": int(plan.recovery_sha256 is not None),
                        }
                    )
                    + "\n"
                )
        else:
            state = asyncio.run(run_learning_curve(args.directory, resume=args.resume))
            sys.stdout.write(
                json.dumps(
                    {
                        "phase": state.phase,
                        "completed": len(state.completed),
                        "updates": len(state.updated),
                    }
                )
                + "\n"
            )
    except Exception as error:
        if args.command == "run" and args.directory.exists():
            number = len(list(args.directory.glob("stop-*.json"))) + 1
            frames = traceback.extract_tb(error.__traceback__)
            with (args.directory / f"stop-{number:03}.json").open("x") as output:
                json.dump(
                    {
                        "error_type": type(error).__name__,
                        "rpc": rpc_diagnostic(error),
                        "frames": [
                            {
                                "file": Path(frame.filename).name,
                                "line": frame.lineno,
                                "function": frame.name,
                            }
                            for frame in frames
                        ],
                        "automatic_retry": False,
                    },
                    output,
                    indent=2,
                )
        sys.stdout.write(
            json.dumps({"error_type": type(error).__name__, "automatic_retry": False}) + "\n"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
