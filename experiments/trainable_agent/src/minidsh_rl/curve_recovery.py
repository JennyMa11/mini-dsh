"""One terminal, pre-sampling init hold may remain charged across a distinct recovery."""

import hashlib
import json
from decimal import Decimal
from http import HTTPStatus
from pathlib import Path
from typing import Literal

from minidsh_rl.cost_store import CostStore, _State
from minidsh_rl.curve_execution import CurveState
from minidsh_rl.curve_schedule import CurvePlan, make_curve_plan
from minidsh_rl.learning_schedule import FrozenRecord, atomic_record, digest
from minidsh_rl.learning_sdk import LeasedResources

INIT_HOLD = Decimal(".10")
LORA_RANK = 16
TRAIN_SEED = 17


class TerminalInitialFailure(FrozenRecord):
    schema_version: Literal[1] = 1
    policy: Literal["terminal_initial_failure_once_v1"] = "terminal_initial_failure_once_v1"
    source_root: str
    finance_directory: str
    operation: str
    original_baseline: Decimal
    training_run_cuid: str
    source_optimizer: str
    remote_metadata: str
    inputs: dict[str, str]
    prior_holds: tuple[str, ...] = ()
    prior_proof: str | None = None


def _failure(root: Path, metadata: Path, *, prior: Path | None = None) -> TerminalInitialFailure:
    plan = CurvePlan.model_validate_json((root / "plan.json").read_bytes())
    state = CurveState.model_validate_json((root / "curve-state.json").read_bytes())
    stop = json.loads((root / "stop-001.json").read_text())
    owned = json.loads(metadata.read_text())
    source = json.loads((root / "source-checkpoint.json").read_text())
    if (
        plan.dataset_version != "curve_dataset_v4"
        or (plan.recovery_sha256 is not None and prior is None)
        or plan.initialization != "restore_optimizer"
        or plan.rows != make_curve_plan(dataset="curve_dataset_v4").rows
        or plan.finance_directory is None
        or plan.budget_baseline is None
        or state.plan_sha256 != digest(root / "plan.json")
        or state.kind != "provider"
        or state.phase != "running"
        or state.pending != "initialize"
        or state.initial is not None
        or state.current is not None
        or state.completed
        or state.groups
        or state.updated
        or state.snapshots
        or any((root / "rollouts").glob("*"))
        or any((root / "updates").glob("*"))
        or any((root / "policies").glob("*"))
        or (root / "initial.json").exists()
        or stop.get("error_type") != "ActorEventSubmitError"
        or stop.get("automatic_retry") is not False
        or not {"_submit_init", "_request"} <= {f["function"] for f in stop["frames"]}
        or owned.get("owned") is not True
        or owned.get("status") != "ENDED"
        or owned.get("checkpoint_count") != 0
        or owned.get("restore_optimizer") is not True
        or owned.get("source_optimizer") != source["optimizer_uri"]
        or owned.get("model") != "Qwen/Qwen3.5-4B"
        or owned.get("lora_rank") != LORA_RANK
        or owned.get("seed") != TRAIN_SEED
        or not owned.get("training_run_cuid")
    ):
        raise ValueError("not a terminal pre-sampling initialization failure")
    scope = hashlib.sha256(str((root / "initial-sdk").resolve()).encode()).hexdigest()[:16]
    operation = f"initial-sdk_{scope}_state_initial"
    snapshot = _State.model_validate_json((root / "budget-after.json").read_bytes())
    pending = {k: v.amount for k, v in snapshot.charges.items() if v.status == "pending"}
    held: tuple[str, ...] = ()
    if prior is not None:
        previous = validate_failure(prior)
        if previous.prior_holds or plan.recovery_sha256 != digest(prior):
            raise ValueError("only one failed restore may precede verified fresh initialization")
        error = json.loads((root / "initial-sdk/state_initial-error.json").read_text())
        client = json.loads((root / "initial-sdk/registered-client.json").read_text())
        if (
            error.get("status_code") != HTTPStatus.NOT_FOUND
            or error.get("code") != "not_found"
            or client["training_run_cuid"] != owned["training_run_cuid"]
        ):
            raise ValueError("distinct failed restore identity changed")
        held = (previous.operation,)
    if pending != dict.fromkeys((*held, operation), INIT_HOLD):
        raise ValueError("initial hold identity or amount changed")
    paths = [
        root / name
        for name in (
            "plan.json",
            "curve-state.json",
            "stop-001.json",
            "source-checkpoint.json",
            "budget-before.json",
            "budget-after.json",
        )
    ]
    paths.append(metadata)
    if prior is not None:
        paths.extend(
            [
                prior,
                root / "initial-sdk/state_initial-error.json",
                root / "initial-sdk/registered-client.json",
            ]
        )
    return TerminalInitialFailure(
        source_root=str(root.resolve()),
        finance_directory=plan.finance_directory,
        operation=operation,
        original_baseline=plan.budget_baseline,
        training_run_cuid=owned["training_run_cuid"],
        source_optimizer=source["optimizer_uri"],
        remote_metadata=str(metadata.resolve()),
        inputs={str(p.resolve()): digest(p) for p in paths},
        prior_holds=held,
        prior_proof=str(prior.resolve()) if prior is not None else None,
    )


def freeze_failure(root: Path, metadata: Path, output: Path) -> TerminalInitialFailure:
    proof = _failure(root, metadata)
    atomic_record(output, proof, create=True)
    return proof


def freeze_failed_restore(root: Path, metadata: Path, output: Path) -> TerminalInitialFailure:
    proof = _failure(root, metadata, prior=root / "recovery-evidence.json")
    atomic_record(output, proof, create=True)
    return proof


def validate_failure(path: Path) -> TerminalInitialFailure:
    proof = TerminalInitialFailure.model_validate_json(path.read_bytes())
    if any(Path(p).is_symlink() or digest(Path(p)) != sha for p, sha in proof.inputs.items()):
        raise ValueError("terminal failure evidence changed")
    if proof != _failure(
        Path(proof.source_root),
        Path(proof.remote_metadata),
        prior=Path(proof.prior_proof) if proof.prior_proof is not None else None,
    ):
        raise ValueError("terminal failure binding changed")
    return proof


class FencedResources(LeasedResources):
    """Keep the old amount/pending; only its terminal init cannot block the new run."""

    def __init__(self, budget: CostStore, proof: Path) -> None:
        super().__init__(budget)
        self.proof = proof

    @property
    def has_pending(self) -> bool:
        _ = self.budget.available
        proof = validate_failure(self.proof)
        if self.budget.path.resolve().parent != Path(proof.finance_directory).resolve():
            raise ValueError("fence financial epoch changed")
        state = _State.model_validate_json(self.budget.path.read_bytes())
        held = (*proof.prior_holds, proof.operation)
        if any(
            key not in state.charges
            or state.charges[key].status != "pending"
            or state.charges[key].amount != INIT_HOLD
            for key in held
        ):
            raise ValueError("terminal initial reservation changed")
        return any(v.status == "pending" and k not in held for k, v in state.charges.items())


def resources_for(root: Path, plan: CurvePlan, budget: CostStore) -> LeasedResources:
    if plan.recovery_sha256 is None:
        return LeasedResources(budget)
    path = root / "recovery-evidence.json"
    if digest(path) != plan.recovery_sha256:
        raise ValueError("frozen recovery evidence changed")
    proof = validate_failure(path)
    if plan.budget_baseline != proof.original_baseline:
        raise ValueError("recovery reset original study budget")
    claim = Path(proof.source_root) / "recovery-claim.json"
    value = json.loads(claim.read_text())
    if value != {"recovery_root": str(root.resolve()), "evidence_sha256": plan.recovery_sha256}:
        raise ValueError("recovery directory already claimed")
    return FencedResources(budget, path)
