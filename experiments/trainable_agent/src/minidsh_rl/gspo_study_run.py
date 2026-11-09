"""Explicit prepare/run for the frozen 40-G4 Base/GSPO efficacy study."""

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
from minidsh_rl.curve_artifacts import verify_hashes
from minidsh_rl.curve_budget import CurveBudget
from minidsh_rl.curve_execution import CurveState, accept_update
from minidsh_rl.curve_recovery import FencedResources, validate_failure
from minidsh_rl.curve_sdk import final_audit
from minidsh_rl.gspo_study import (
    GROUP_UPPER,
    GROUPS,
    StudyPlan,
    evaluation_remaining,
    execute_study,
    make_study,
)
from minidsh_rl.gspo_study_runtime import GspoStudyRuntime, ProviderRuntime
from minidsh_rl.harness_comparison import _require_gates
from minidsh_rl.learning_gates import PROJECT, _sources
from minidsh_rl.learning_run import ExecutionConfig
from minidsh_rl.learning_schedule import atomic_record, digest
from minidsh_rl.rpc_diagnostics import rpc_diagnostic
from minidsh_rl.train_run import _require_core_gates

BASE = PROJECT / "benchmark-results/trainable_agent"
IMAGE = (
    "python:3.12-slim-bookworm@sha256:"
    "e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337"
)
KERNELS = ("gspo.py", "gspo_trainer.py", "datum.py", "contracts.py", "gspo_numeric.py")


def numerical_inputs() -> dict[str, str]:
    folder = BASE / "lab-migration/gspo-smoke-02"
    manifest = folder / "source.json"
    old = json.loads(manifest.read_text())
    inputs = {str(manifest): digest(manifest)}
    for name in KERNELS:
        current = PROJECT / "experiments/trainable_agent/src/minidsh_rl" / name
        entry = next(k for k in old if k.endswith("/" + name))
        if digest(current) != old[entry]:
            raise ValueError("GSPO numerical kernel changed")
    result = folder / "gspo-smoke-02-numeric/result.json"
    evidence = json.loads(result.read_text())
    if (
        evidence["passed"] is not True
        or evidence["mask_passed"] is not True
        or evidence["reduction"] != "sum"
    ):
        raise ValueError("GSPO gradient probe not verified")
    inputs[str(result)] = digest(result)
    return inputs


def prepare(root: Path) -> StudyPlan:
    _require_core_gates()
    _require_gates(IMAGE)
    expected = make_study()
    finance = BASE / "learning-curve-v2/run-01/finance"
    fence = BASE / "learning-curve-v4/run-03/recovery-evidence.json"
    validate_failure(fence)
    inputs = numerical_inputs()
    config_path = BASE / "development-feedback-v3/run-01/execution-config.json"
    config = ExecutionConfig.model_validate_json(config_path.read_bytes())
    reference = BASE / "expansion-v2/pilot-train-01/weight-evidence/initial.archive"
    inputs.update(
        {
            str(p): digest(p)
            for p in (
                fence,
                config_path,
                reference,
                BASE / "expansion-v2/phase-a/task-gates-01/summary.json",
                BASE / "expansion-v2/phase-a/task-gates-01/manifest.json",
            )
        }
    )
    root.mkdir(parents=True, exist_ok=False)
    (root / "recovery-evidence.json").write_bytes(fence.read_bytes())
    (root / "reference.json").write_text(
        json.dumps({"archive": str(reference), "sha256": digest(reference)})
    )
    with CurveBudget(finance) as budget:
        resources = FencedResources(budget, root / "recovery-evidence.json")
        state = CurveState(plan_sha256="pending", kind="provider")
        if resources.has_pending or resources.available < GROUP_UPPER + evaluation_remaining(
            expected, state
        ):
            raise ValueError("first full group plus all paired evaluation unavailable")
        (root / "budget-before.json").write_bytes(budget.path.read_bytes())
    inputs.update(
        {
            str(root / name): digest(root / name)
            for name in ("reference.json", "recovery-evidence.json", "budget-before.json")
        }
    )
    plan = expected.model_copy(
        update={
            "sources": _sources(),
            "inputs": inputs,
            "finance": str(finance),
            "image": IMAGE,
            "tokenizer_sha256": config.tokenizer_sha256,
        }
    )
    atomic_record(root / "plan.json", plan, create=True)
    with tarfile.open(root / "source.tar.gz", "w:gz") as archive:
        for name in plan.sources:
            archive.add(name, arcname=str(Path(name).relative_to(PROJECT)), recursive=False)
    return plan


def load_study(root: Path) -> StudyPlan:
    plan = StudyPlan.model_validate_json((root / "plan.json").read_bytes())
    expected = make_study()
    if (
        plan.rows != expected.rows
        or plan.cases != expected.cases
        or plan.sources != _sources()
        or plan.image != IMAGE
    ):
        raise ValueError("frozen sources/scientific queue changed")
    if any(Path(p).is_symlink() or digest(Path(p)) != sha for p, sha in plan.inputs.items()):
        raise ValueError("frozen input evidence changed")
    numerical_inputs()
    validate_failure(root / "recovery-evidence.json")
    return plan


async def run(root: Path, *, resume: bool = False) -> CurveState:
    plan = load_study(root)
    with CurveBudget(Path(plan.finance)) as budget:
        resources = FencedResources(budget, root / "recovery-evidence.json")
        if resources.has_pending:
            raise ValueError("unknown current request; no replay")
        state_path = root / "state.json"
        existing = (
            CurveState.model_validate_json(state_path.read_bytes()) if state_path.exists() else None
        )
        if existing is not None:
            if existing.kind != "provider" or existing.plan_sha256 != digest(root / "plan.json"):
                raise ValueError("provider state provenance changed")
            verify_hashes(root, existing.artifacts)
            if existing.phase != "running":
                return existing
            if not resume or existing.pending not in {None, "update"}:
                raise ValueError("explicit confirmed-boundary resume required")
            if existing.pending == "update":
                accept_update(root, existing.model_copy(deep=True), existing.key)
        admission = existing or CurveState(plan_sha256=digest(root / "plan.json"), kind="provider")
        needed = evaluation_remaining(plan, admission)
        if len(admission.groups) < GROUPS:
            needed += GROUP_UPPER
        if admission.initial is None:
            needed += Decimal(".25")
        if resources.available < needed:
            raise ValueError("next whole stage and all evaluation unavailable")
        host = LocalCommandRunner(WorkspaceSandbox(root), CommandPolicy(frozenset({"docker"})))
        runtime = GspoStudyRuntime(
            trio.ServiceClient(), budget, root, host, plan.tokenizer_sha256, image=plan.image
        )
        try:
            state = await execute_study(
                root, plan, resources, ProviderRuntime(runtime), resume=resume
            )
            if state.phase == "finished" and state.updated and state.current is not None:
                await final_audit(runtime, state.current)
            return state
        finally:
            (root / "budget-after.json").write_bytes(budget.path.read_bytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "inspect", "run"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    for name in ("httpx", "httpcore", "pytrio"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    value: dict[str, object]
    try:
        if args.command == "prepare":
            plan = prepare(args.directory)
            value = {"prepared": True, "declared": len(plan.rows), "groups": 40}
        elif args.command == "inspect":
            plan = load_study(args.directory)
            value = {
                "frozen": True,
                "declared": len(plan.rows),
                "test_previously_opened": plan.test_previously_opened,
            }
        else:
            state = asyncio.run(run(args.directory, resume=args.resume))
            value = {
                "phase": state.phase,
                "completed": len(state.completed),
                "updates": len(state.updated),
            }
        sys.stdout.write(json.dumps(value) + "\n")
    except Exception as error:
        if args.directory.exists():
            number = len(list(args.directory.glob("stop-*.json"))) + 1
            with (args.directory / f"stop-{number:03}.json").open("x") as file:
                json.dump(
                    {
                        "rpc": rpc_diagnostic(error),
                        "frames": [
                            {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                            for f in traceback.extract_tb(error.__traceback__)
                        ],
                    },
                    file,
                    indent=2,
                )
        sys.stdout.write(
            json.dumps({"error_type": type(error).__name__, "automatic_retry": False}) + "\n"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
