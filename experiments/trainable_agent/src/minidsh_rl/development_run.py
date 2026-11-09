"""Freeze and run exactly 24 development samples using an existing zero-LoRA policy."""

import argparse
import asyncio
import json
import logging
import sys
import tarfile
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytrio as trio  # type: ignore[import-untyped]
from pytrio.config.config import auth_state  # type: ignore[import-untyped]
from pytrio.transport.control.http_client import ControlHTTPClient  # type: ignore[import-untyped]

from minidsh.infrastructure.process import CommandPolicy, LocalCommandRunner
from minidsh.infrastructure.sandbox import WorkspaceSandbox
from minidsh_rl.backend_v2 import Tokenizer
from minidsh_rl.billing_calibration import price_card
from minidsh_rl.curve_budget import CurveBudget
from minidsh_rl.curve_run import validate_config, validate_data
from minidsh_rl.curve_schedule import CurveRow
from minidsh_rl.curve_sdk import FinancialBackend
from minidsh_rl.development_budget import StudyBudget
from minidsh_rl.development_cases import development_cases
from minidsh_rl.development_execution import DevelopmentState, execute_development
from minidsh_rl.development_schedule import DevelopmentPlan, make_development_plan
from minidsh_rl.learning_components import verify_components
from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.learning_execution import Checkpoint
from minidsh_rl.learning_feedback import verify_feedback
from minidsh_rl.learning_gates import PROJECT, _sources, audit
from minidsh_rl.learning_initial import validate_initial_adapter
from minidsh_rl.learning_rollout import LearningRollout, run_learning_rollout
from minidsh_rl.learning_run import ExecutionConfig
from minidsh_rl.learning_schedule import atomic_record, digest
from minidsh_rl.learning_sdk import LeasedResources, SavedPair, _binding, tokenizer_pin
from minidsh_rl.preflight import service_evidence


async def owned_metadata(pair: SavedPair) -> dict[str, object]:
    """Read-only ownership/URI check; no trainer, optimizer, export or new checkpoint."""
    client = ControlHTTPClient(api_key=auth_state.api_key)
    try:
        auth = await client.verify_api_key()
        run = await client.get_training_run(pair.training_run_cuid, access_scope="owned")
        listing = await client.list_training_run_checkpoints(pair.training_run_cuid)
        matches = [cp for cp in listing.checkpoints if cp.checkpoint_uri == pair.sampler.path]
        if len(matches) != 1:
            raise ValueError("source checkpoint not found")
        cp = matches[0]
        if (
            run.workspace_cuid != auth.workspace_cuid
            or run.base_model != "Qwen/Qwen3.5-4B"
            or cp.status != "READY"
            or cp.training_run_cuid != pair.training_run_cuid
            or cp.workspace_cuid not in (None, auth.workspace_cuid)
            or f"/{auth.workspace_cuid}/{pair.training_run_cuid}/" not in cp.path
        ):
            raise ValueError("source ownership/checkpoint identity mismatch")
        return {
            "owned": True,
            "model": run.base_model,
            "source_uri": pair.sampler.path,
            "training_run_cuid": pair.training_run_cuid,
            "checkpoint_id": cp.checkpoint_cuid,
            "new_training_clients": 0,
            "new_checkpoints": 0,
        }
    finally:
        await client.aclose()


def prepare_development(root: Path, data: Path, source: Path, finance: Path) -> DevelopmentPlan:
    """Only local validation and snapshots; no SDK service or account connection."""
    validate_data(data, development_cases())
    config_path = source / "execution-config.json"
    config = ExecutionConfig.model_validate_json(config_path.read_bytes())
    validate_config(config)
    cp_path = source / "initial.json"
    checkpoint = Checkpoint.model_validate_json(cp_path.read_bytes())
    pair_path = source / "policies" / (checkpoint.policy.policy_id + ".json")
    pair = SavedPair.model_validate_json(pair_path.read_bytes())
    archive_path = source / "initial-sdk/initial.archive"
    audit_path = source / "initial-sdk/curve-initial-audit.json"
    proof = json.loads(audit_path.read_text())
    if (
        checkpoint.execution_kind != "provider"
        or checkpoint.policy != _binding(pair)
        or checkpoint.sampler_uri != pair.sampler.path
        or checkpoint.optimizer_uri != pair.optimizer.path
        or checkpoint.policy.tokenizer_sha256 != config.tokenizer_sha256
        or proof["archive_sha256"] != digest(archive_path)
        or proof["checkpoint"]["sampler_uri"] != checkpoint.sampler_uri
    ):
        raise ValueError("immutable source policy evidence mismatch")
    components = verify_components(config.components.verify())
    metrics = validate_initial_adapter(components.inputs["baseline"].verify(), archive_path)
    with CurveBudget(finance) as budget:
        if LeasedResources(budget).has_pending:
            raise ValueError("financial pending blocks a new study")
        if budget.available < make_development_plan().required_cny:
            raise ValueError("complete development allocation unavailable")
        baseline = budget.committed
    root.mkdir(parents=True, exist_ok=False)
    (root / "data-summary.json").write_bytes(data.read_bytes())
    (root / "execution-config.json").write_bytes(config_path.read_bytes())
    atomic_record(root / "source-checkpoint.json", checkpoint, create=True)
    atomic_record(root / "source-pair.json", pair, create=True)
    (root / "initial-evidence.json").write_text(
        json.dumps(
            {
                "metrics": metrics,
                "archive_sha256": digest(archive_path),
                "source_initial_audit_sha256": digest(audit_path),
            }
        )
    )
    inputs = {
        str(p.resolve()): digest(p)
        for p in (cp_path, pair_path, archive_path, audit_path, config_path)
    }
    inputs.update(
        {
            str(p.resolve()): digest(p)
            for p in (
                root / "data-summary.json",
                root / "execution-config.json",
                root / "source-checkpoint.json",
                root / "source-pair.json",
                root / "initial-evidence.json",
            )
        }
    )
    plan = make_development_plan().model_copy(
        update={
            "sources": _sources(),
            "inputs": inputs,
            "finance_directory": str(finance.resolve()),
            "start_committed": baseline,
        }
    )
    atomic_record(root / "plan.json", plan, create=True)
    with tarfile.open(root / "source.tar.gz", "w:gz") as output:
        for name in plan.sources:
            output.add(name, arcname=str(Path(name).relative_to(PROJECT)), recursive=False)
    return plan


def load_development(root: Path) -> tuple[DevelopmentPlan, ExecutionConfig, Checkpoint, SavedPair]:
    plan = DevelopmentPlan.model_validate_json((root / "plan.json").read_bytes())
    expected = make_development_plan()
    if (
        plan.kind != "provider"
        or plan.rows != expected.rows
        or plan.case_sha256 != expected.case_sha256
        or plan.required_cny != expected.required_cny
        or plan.sources != _sources()
        or plan.start_committed is None
    ):
        raise ValueError("frozen development source/queue changed")
    if any(
        Path(name).is_symlink() or digest(Path(name)) != value
        for name, value in plan.inputs.items()
    ):
        raise ValueError("frozen input evidence changed")
    validate_data(root / "data-summary.json", development_cases())
    config = ExecutionConfig.model_validate_json((root / "execution-config.json").read_bytes())
    validate_config(config)
    checkpoint = Checkpoint.model_validate_json((root / "source-checkpoint.json").read_bytes())
    pair = SavedPair.model_validate_json((root / "source-pair.json").read_bytes())
    if checkpoint.policy != _binding(pair) or checkpoint.sampler_uri != pair.sampler.path:
        raise ValueError("frozen source policy changed")
    return plan, config, checkpoint, pair


async def run_development(root: Path) -> DevelopmentState:
    plan, config, checkpoint, pair = load_development(root)
    if (root / "state.json").exists():
        state = DevelopmentState.model_validate_json((root / "state.json").read_bytes())
        if state.phase != "running":
            return state
        raise ValueError("explicit unknown-outcome audit required; no automatic replay")
    with CurveBudget(Path(plan.finance_directory)) as parent:
        if parent.committed != plan.start_committed or LeasedResources(parent).has_pending:
            raise ValueError("financial baseline changed or pending")
        budget = StudyBudget(parent, parent.committed)
        if budget.available < plan.required_cny:
            raise ValueError("complete development allocation unavailable")
        started = root / "started.json"
        with started.open("x") as file:
            json.dump(
                {
                    "plan_sha256": digest(root / "plan.json"),
                    "start_committed": str(parent.committed),
                },
                file,
            )
        (root / "budget-before.json").write_bytes(parent.path.read_bytes())
        prefix = "dev3_" + digest(root / "plan.json")[:16]
        parent.reserve(prefix + "_metadata", Decimal(".05"))
        await service_evidence(root / "live-preflight.json")
        price_card(root / "live-preflight.json")
        metadata = await owned_metadata(pair)
        (root / "owned-metadata.json").write_text(json.dumps(metadata, indent=2))
        parent.settle(prefix + "_metadata", Decimal(".05"))
        load_development(root)
        service = trio.ServiceClient()
        parent.reserve(prefix + "_connect", Decimal(".01"))
        sampler = await asyncio.to_thread(
            service.create_sampling_client,
            base_model="Qwen/Qwen3.5-4B",
            model_path=checkpoint.sampler_uri,
        )
        tokenizer = cast(Tokenizer, sampler.get_tokenizer())
        if (
            sampler.model_id != "Qwen/Qwen3.5-4B"
            or tokenizer_pin(tokenizer) != config.tokenizer_sha256
        ):
            raise ValueError("live sampler model/tokenizer mismatch")
        parent.settle(prefix + "_connect", Decimal(".01"))
        (root / "sampling-connection.json").write_text(
            json.dumps(
                {
                    "sampling_run_cuid": sampler.sampling_run_cuid,
                    "source_training_run_cuid": pair.training_run_cuid,
                    "sampler_uri": pair.sampler.path,
                    "policy": checkpoint.policy.model_dump(),
                    "new_training_clients": 0,
                }
            )
        )
        host = LocalCommandRunner(WorkspaceSandbox(root), CommandPolicy(frozenset({"docker"})))

        async def sample(case: LearningCase, row: CurveRow, output: Path) -> LearningRollout:
            backend = FinancialBackend(sampler, tokenizer, budget, checkpoint.policy.policy_id)
            backend.directory = output
            return await run_learning_rollout(
                case,
                backend,
                policy=checkpoint.policy,
                run_id=row.run_id,
                seed=row.seed,
                output=output,
                host=host,
                image=config.image,
            )

        async def feedback(value: LearningRollout, output: Path) -> bool:
            return await verify_feedback(
                value, root / "rollouts" / value.run_id, output, host, config.image
            )

        state = await execute_development(root, plan, budget, checkpoint.policy, sample, feedback)
        if LeasedResources(parent).has_pending:
            raise ValueError("study finished with unresolved financial calls")
        (root / "budget-after.json").write_bytes(parent.path.read_bytes())
        return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit", "prepare", "inspect", "run"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--finance", type=Path, required=True)
    args = parser.parse_args()
    for name in ("httpx", "httpcore", "pytrio"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        if args.command == "audit":
            asyncio.run(
                audit(
                    development_cases(),
                    args.directory,
                    args.finance / "budget.json",
                    policy="curve_pure_v1",
                )
            )
        elif args.command == "prepare":
            if args.data is None or args.source is None:
                parser.error("prepare requires --data and --source")
            plan = prepare_development(args.directory, args.data, args.source, args.finance)
            sys.stdout.write(
                json.dumps(
                    {
                        "rows": len(plan.rows),
                        "required_cny": str(plan.required_cny),
                        "cap_cny": plan.cap_cny,
                    }
                )
                + "\n"
            )
        elif args.command == "inspect":
            plan, _, _, _ = load_development(args.directory)
            with CurveBudget(Path(plan.finance_directory)) as budget:
                sys.stdout.write(
                    json.dumps(
                        {
                            "available_cny": str(budget.available),
                            "required_cny": str(plan.required_cny),
                            "kind": "sampling_only",
                        }
                    )
                    + "\n"
                )
        else:
            state = asyncio.run(run_development(args.directory))
            sys.stdout.write(
                json.dumps(
                    {
                        "phase": state.phase,
                        "completed": len(state.completed),
                        "declared": 24,
                        "training_updates": 0,
                    }
                )
                + "\n"
            )
    except Exception as error:
        sys.stdout.write(
            json.dumps({"error_type": type(error).__name__, "automatic_retry": False}) + "\n"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
