"""One terminal backward-only rollback to an exactly restored optimizer checkpoint."""

import argparse
import asyncio
import hashlib
import io
import json
import logging
import shutil
import ssl
import sys
import tarfile
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytrio as trio  # type: ignore[import-untyped]
import torch
from pytrio.config.config import auth_state  # type: ignore[import-untyped]
from pytrio.transport.control.http_client import ControlHTTPClient  # type: ignore[import-untyped]

from minidsh.infrastructure.process import CommandPolicy, LocalCommandRunner
from minidsh.infrastructure.sandbox import WorkspaceSandbox
from minidsh_rl.analyze_v2 import turns
from minidsh_rl.contracts import advantages
from minidsh_rl.cost_store import _State
from minidsh_rl.curve_budget import CurveBudget
from minidsh_rl.curve_execution import MAX_RESUMES, CurveState, accept_update
from minidsh_rl.curve_recovery import LORA_RANK, TRAIN_SEED, FencedResources, validate_failure
from minidsh_rl.curve_sdk import final_audit
from minidsh_rl.gspo import build_gspo_batch
from minidsh_rl.gspo_study import GROUP_UPPER, StudyPlan, evaluation_remaining, execute_study
from minidsh_rl.gspo_study_analysis import audit_loss
from minidsh_rl.gspo_study_run import load_study
from minidsh_rl.gspo_study_runtime import GspoStudyRuntime, ProviderRuntime
from minidsh_rl.learning_execution import Checkpoint
from minidsh_rl.learning_gates import _sources
from minidsh_rl.learning_schedule import atomic_record, digest
from minidsh_rl.rpc_diagnostics import rpc_diagnostic
from minidsh_rl.weight_probe import weight_delta

MAX_BYTES = 256 * 1024 * 1024
READINESS_ATTEMPTS = 60
READINESS_DELAY = 5


def eligible(state: CurveState, journal: dict[str, Any], metadata: dict[str, Any]) -> None:
    if (
        state.kind != "provider"
        or state.phase != "running"
        or state.pending != "update"
        or state.current is None
        or state.initial is None
        or journal.get("state") != "backward_submitted"
        or metadata.get("owned") is not True
        or metadata.get("status") != "ENDED"
        or metadata.get("corrupted") is not False
        or metadata.get("base_model") != "Qwen/Qwen3.5-4B"
        or metadata.get("seed") != TRAIN_SEED
        or metadata.get("lora_rank") != LORA_RANK
    ):
        raise ValueError("only terminal owned backward-only failure may roll back")


def optimizer(path: Path) -> dict[str, Any]:
    with tarfile.open(path) as archive:
        items = [m for m in archive.getmembers() if m.isfile() and m.name == "optimizer.pt"]
        if len(items) != 1 or items[0].size > MAX_BYTES:
            raise ValueError("missing or oversized optimizer")
        stream = archive.extractfile(items[0])
        if stream is None:
            raise ValueError("missing optimizer state")
        value = torch.load(io.BytesIO(stream.read()), weights_only=True, map_location="cpu")
    if not isinstance(value, dict) or set(value) != {"state", "param_groups"} or not value["state"]:
        raise ValueError("nonempty trained optimizer required")
    return value


def equal_state(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, torch.Tensor) and isinstance(right, torch.Tensor):
        return left.dtype == right.dtype and left.shape == right.shape and torch.equal(left, right)
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            equal_state(v, right[k]) for k, v in left.items()
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            equal_state(a, b) for a, b in zip(left, right, strict=True)
        )
    return bool(left == right)


def verify_failure(old: Path) -> tuple[StudyPlan, CurveState, str, Decimal]:
    plan = StudyPlan.model_validate_json((old / "plan.json").read_bytes())
    state = CurveState.model_validate_json((old / "state.json").read_bytes())
    if any(digest(Path(p)) != h for p, h in plan.sources.items()):
        raise ValueError("original frozen source changed")
    if state.plan_sha256 != digest(old / "plan.json"):
        raise ValueError("original controller binding changed")
    slot = int(state.key.rsplit("_", 1)[-1])
    folder = old / "updates" / state.key
    journal = json.loads((folder / f"update_{slot:03}.json").read_text())
    metadata = json.loads((old / "terminal-inspection.json").read_text())
    eligible(state, journal, metadata)
    if state.current is None:
        raise ValueError("confirmed checkpoint required")
    registered = json.loads((old / "initial-sdk/registered-client.json").read_text())
    if metadata["training_run_cuid"] != registered["training_run_cuid"]:
        raise ValueError("terminal task identity differs")
    stop = json.loads((old / "stop-001.json").read_text())
    if stop["rpc"]["error_type"] != "EventPollingError" or any(
        (folder / name).exists() for name in (f"optim_{slot:03}.json", "receipt.json")
    ):
        raise ValueError("unknown optimizer or non-query failure cannot roll back")
    if (
        state.groups != [f"train_{i:03}" for i in range(slot)]
        or len(state.completed) != (slot + 1) * 4
    ):
        raise ValueError("only one fully sampled pending G4 may be carried")
    if state.completed != [r.run_id for r in plan.rows[: len(state.completed)]]:
        raise ValueError("frozen trajectory queue changed")
    replay = CurveState(plan_sha256=state.plan_sha256, kind="provider", current=state.initial)
    for key in state.groups:
        accept_update(old, replay, key)
    if replay.current != state.current or replay.updated != state.updated:
        raise ValueError("last confirmed optimizer chain differs")
    ready = {
        c["checkpoint_uri"]
        for c in metadata["checkpoint_listing"]["checkpoints"]
        if c["status"] == "READY"
    }
    if not {state.current.sampler_uri, state.current.optimizer_uri} <= ready:
        raise ValueError("last confirmed checkpoints not ready")
    scope = hashlib.sha256(str(folder.resolve()).encode()).hexdigest()[:16]
    operation = folder.name + "_" + scope + f"_custom_fb_{slot:03}"
    ledger = _State.model_validate_json((old / "budget-after.json").read_bytes())
    if ledger.charges[operation].status != "pending":
        raise ValueError("original unknown backward reservation changed")
    return plan, state, operation, ledger.charges[operation].amount


async def ready_checkpoint(client: Any, uid: str, uri: str) -> Any:
    for _ in range(READINESS_ATTEMPTS):
        listing = await client.list_training_run_checkpoints(uid)
        found = [c for c in listing.checkpoints if c.checkpoint_uri == uri and c.status == "READY"]
        if len(found) == 1:
            return found[0]
        await asyncio.sleep(READINESS_DELAY)
    raise ValueError("trained checkpoint readiness deadline")


async def download_trained(uid: str, uri: str, output: Path) -> None:
    """Trained Adam archives need a fixed 256 MiB bound, verified HTTPS, owned URI."""
    if await asyncio.to_thread(output.exists) or await asyncio.to_thread(output.is_symlink):
        raise ValueError("fresh archive required")
    client = ControlHTTPClient(api_key=auth_state.api_key)
    try:
        auth = await client.verify_api_key()
        run = await client.get_training_run(uid, access_scope="owned")
        if run.workspace_cuid != auth.workspace_cuid or run.base_model != "Qwen/Qwen3.5-4B":
            raise ValueError("checkpoint owner/model differs")
        checkpoint = await ready_checkpoint(client, uid, uri)
        if (
            checkpoint.training_run_cuid != uid
            or checkpoint.workspace_cuid not in (None, auth.workspace_cuid)
            or f"/{auth.workspace_cuid}/{uid}/" not in checkpoint.path
            or (checkpoint.size_bytes or 0) > MAX_BYTES
        ):
            raise ValueError("checkpoint association/size rejected")
        response = await client.get_checkpoint_download_url(checkpoint.checkpoint_cuid)
        if urlparse(response.url).scheme != "https":
            raise ValueError("HTTPS required")
        context = ssl.create_default_context()
        if response.tls_cert_pem:
            context.load_verify_locations(cadata=response.tls_cert_pem)
        temporary = output.with_suffix(".partial")
        count = 0
        async with (
            httpx.AsyncClient(verify=context, timeout=60, follow_redirects=False) as transport,
            transport.stream("GET", response.url) as stream,
        ):
            stream.raise_for_status()
            with temporary.open("xb") as file:
                temporary.chmod(0o600)
                async for block in stream.aiter_bytes():
                    count += len(block)
                    if count > MAX_BYTES:
                        raise ValueError("trained archive exceeds size bound")
                    file.write(block)
        if not count:
            raise ValueError("empty archive")
        temporary.replace(output)
    finally:
        await client.aclose()


class RecoveryResources(FencedResources):
    def __init__(self, budget: CurveBudget, root: Path) -> None:
        super().__init__(budget, root / "recovery-evidence.json")
        self.root = root

    @property
    def has_pending(self) -> bool:
        _ = super().has_pending
        proof = json.loads((self.root / "rollback-proof.json").read_text())
        if any(digest(Path(p)) != h for p, h in proof["inputs"].items()):
            raise ValueError("rollback evidence changed")
        _, _, operation, amount = verify_failure(Path(proof["source_root"]))
        legacy = validate_failure(self.proof)
        state = _State.model_validate_json(self.budget.path.read_bytes())
        if (
            state.charges[operation].status != "pending"
            or state.charges[operation].amount != amount
        ):
            raise ValueError("old backward reservation changed")
        held = {*legacy.prior_holds, legacy.operation, operation}
        return any(v.status == "pending" and k not in held for k, v in state.charges.items())


def prepare(old: Path, root: Path) -> None:
    plan, state, operation, amount = verify_failure(old)
    paths = [
        old / name
        for name in (
            "plan.json",
            "state.json",
            "stop-001.json",
            "terminal-inspection.json",
            "budget-after.json",
        )
    ]
    paths.extend(old / p for p in state.artifacts)
    folder = old / "updates" / state.key
    paths.extend(p for p in folder.rglob("*") if p.is_file())
    inputs = {str(p.resolve()): digest(p) for p in paths}
    proof = {
        "source_root": str(old.resolve()),
        "inputs": inputs,
        "operation": operation,
        "amount": str(amount),
    }
    root.mkdir(parents=True, exist_ok=False)
    for name in ("initial-sdk", "policies", "rollouts", "updates"):
        shutil.copytree(old / name, root / name)
    # The original uncertain transaction remains immutable in run-01.
    shutil.rmtree(root / "updates" / state.key)
    (root / "updates" / state.key).mkdir()
    for name in ("group.json", "rewards.json"):
        shutil.copyfile(folder / name, root / "updates" / state.key / name)
    for name in ("initial.json", "reference.json", "recovery-evidence.json"):
        shutil.copyfile(old / name, root / name)
    (root / "rollback-proof.json").write_text(json.dumps(proof, indent=2))
    new_inputs = dict(plan.inputs)
    new_inputs[str(root.resolve() / "rollback-proof.json")] = digest(root / "rollback-proof.json")
    new = plan.model_copy(update={"sources": _sources(), "inputs": new_inputs})
    atomic_record(root / "plan.json", new, create=True)
    with tarfile.open(root / "source.tar.gz", "w:gz") as archive:
        for name in new.sources:
            archive.add(name, arcname=str(Path(name).relative_to(Path.cwd())), recursive=False)
    state.plan_sha256 = digest(root / "plan.json")
    atomic_record(root / "state.json", state, create=True)


def prepare_readiness_resume(root: Path) -> None:
    """Amend only recovery IO before any resumed update, preserving the old freeze."""
    plan = StudyPlan.model_validate_json((root / "plan.json").read_bytes())
    state = CurveState.model_validate_json((root / "state.json").read_bytes())
    own_source = str(Path(__file__).resolve())
    if any(digest(Path(p)) != h for p, h in plan.sources.items() if p != own_source):
        raise ValueError("only readiness adapter may change before resumed updates")
    proof = json.loads((root / "rollback-proof.json").read_text())
    _, original, _, _ = verify_failure(Path(proof["source_root"]))
    if (
        state.groups != original.groups
        or state.updated != original.updated
        or state.pending != "update"
    ):
        raise ValueError("learning already resumed or transaction changed")
    folder = root / "rollback-sdk"
    audit = json.loads((folder / "readiness-audit.json").read_text())
    if (
        audit["reference_sha256"] != digest(folder / "reference.archive")
        or audit["restored_sha256"] != digest(folder / "restored.archive")
        or weight_delta(folder / "reference.archive", folder / "restored.archive") != 0
        or not equal_state(
            optimizer(folder / "reference.archive"), optimizer(folder / "restored.archive")
        )
    ):
        raise ValueError("readiness pause requires proven full optimizer equivalence")
    old_plan = root / "plan-before-readiness-fix.json"
    with old_plan.open("xb") as output:
        output.write((root / "plan.json").read_bytes())
    old_archive = root / "source-before-readiness-fix.tar.gz"
    (root / "source.tar.gz").rename(old_archive)
    inputs = dict(plan.inputs)
    inputs.update(
        {
            str(p.resolve()): digest(p)
            for p in (
                old_plan,
                old_archive,
                folder / "readiness-audit.json",
                folder / "reference.archive",
                folder / "restored.archive",
            )
        }
    )
    new = plan.model_copy(update={"sources": _sources(), "inputs": inputs})
    atomic_record(root / "plan.json", new)
    with tarfile.open(root / "source.tar.gz", "w:gz") as archive:
        for name in new.sources:
            archive.add(name, arcname=str(Path(name).relative_to(Path.cwd())), recursive=False)
    state.plan_sha256 = digest(root / "plan.json")
    atomic_record(root / "state.json", state)


class ConnectedRuntime(ProviderRuntime):
    async def reconnect(self, before: Checkpoint, attempt: int) -> None:
        if before != self.runtime.current or self.runtime.client is None or attempt != MAX_RESUMES:
            raise ValueError("unexpected repeated restore")


async def run(root: Path, *, resume_ready: bool = False) -> CurveState:
    plan = load_study(root)
    state = CurveState.model_validate_json((root / "state.json").read_bytes())
    if state.current is None:
        raise ValueError("confirmed optimizer required")
    if resume_ready and not (root / "plan-before-readiness-fix.json").is_file():
        raise ValueError("verified readiness amendment required")
    with CurveBudget(Path(plan.finance)) as budget:
        resources = RecoveryResources(budget, root)
        if resources.has_pending or resources.available < GROUP_UPPER + evaluation_remaining(
            plan, state
        ) + Decimal("1"):
            raise ValueError("unknown request or incomplete recovery stage admission")
        folder = root / ("rollback-sdk-resume" if resume_ready else "rollback-sdk")
        folder.mkdir(exist_ok=False)
        host = LocalCommandRunner(WorkspaceSandbox(root), CommandPolicy(frozenset({"docker"})))
        runtime = GspoStudyRuntime(
            trio.ServiceClient(), budget, root, host, plan.tokenizer_sha256, image=plan.image
        )
        try:
            atomic_record(folder / "started.json", state.current, create=True)
            await runtime.reconnect(state.current, MAX_RESUMES if resume_ready else 1)
            # Export both sides; verify actual nonempty optimizer, not just a URI/flag.
            old_pair = runtime._pair(state.current)
            await download_trained(
                old_pair.training_run_cuid,
                state.current.optimizer_uri,
                folder / "reference.archive",
            )
            restored = await asyncio.to_thread(runtime._save_pair, folder, "rollback_audit")
            new_pair = runtime._pair(restored)
            await download_trained(
                new_pair.training_run_cuid, restored.optimizer_uri, folder / "restored.archive"
            )
            delta = weight_delta(folder / "reference.archive", folder / "restored.archive")
            if delta != 0 or not equal_state(
                optimizer(folder / "reference.archive"), optimizer(folder / "restored.archive")
            ):
                raise ValueError("restored trained weights/optimizer differ; never fresh-start")
            (folder / "verified.json").write_text(
                json.dumps(
                    {
                        "adapter_delta_l2": delta,
                        "optimizer_equal": True,
                        "reference_sha256": digest(folder / "reference.archive"),
                        "restored_sha256": digest(folder / "restored.archive"),
                    }
                )
            )
            update = root / "updates" / state.key
            raw = json.loads((update / "group.json").read_text())
            rewards = tuple(
                float(json.loads((root / "rollouts" / name / "result.json").read_text())["success"])
                for name in raw["run_ids"]
            )
            batch = build_gspo_batch(
                tuple(turns(root / "rollouts" / name) for name in raw["run_ids"]),
                advantages(rewards),
            )
            await runtime.update_gspo(
                state.key, batch, state.current, update, evaluation_remaining(plan, state)
            )
            audit_loss(update, int(state.key.rsplit("_", 1)[-1]), batch)
            accept_update(root, state, state.key)
            state.resumes = 1
            atomic_record(root / "state.json", state)
            result = await execute_study(
                root, plan, resources, ConnectedRuntime(runtime), resume=True
            )
            if result.phase == "finished" and result.current is not None:
                await final_audit(runtime, result.current)
            return result
        finally:
            (root / "budget-after.json").write_bytes(budget.path.read_bytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "resume-prepare", "run"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--resume-ready", action="store_true")
    args = parser.parse_args()
    for name in ("httpx", "httpcore", "pytrio"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        if args.command == "prepare":
            if args.source is None:
                raise ValueError("source required")
            prepare(args.source, args.directory)
            value: dict[str, object] = {"prepared": True, "declared": 352}
        elif args.command == "resume-prepare":
            prepare_readiness_resume(args.directory)
            value = {"readiness_resume_prepared": True, "declared": 352}
        else:
            state = asyncio.run(run(args.directory, resume_ready=args.resume_ready))
            value = {
                "phase": state.phase,
                "completed": len(state.completed),
                "updates": len(state.updated),
            }
        sys.stdout.write(json.dumps(value) + "\n")
    except Exception as error:
        if args.directory.exists():
            (args.directory / "rollback-error.json").write_text(
                json.dumps(rpc_diagnostic(error), indent=2)
            )
        sys.stdout.write(
            json.dumps({"error_type": type(error).__name__, "automatic_retry": False}) + "\n"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
