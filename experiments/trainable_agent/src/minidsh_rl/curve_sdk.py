"""Curve provider adapter: original loss kernel, explicit profile, financial bindings."""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from minidsh_rl.cost_store import _State
from minidsh_rl.curve_execution import MAX_RESUMES
from minidsh_rl.curve_schedule import CurveRow
from minidsh_rl.curve_weights import download_owned
from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.learning_execution import Checkpoint
from minidsh_rl.learning_initial import validate_initial_adapter
from minidsh_rl.learning_rollout import LearningRollout, run_learning_rollout
from minidsh_rl.learning_schedule import atomic_record, digest
from minidsh_rl.learning_sdk import LearningSDKRuntime
from minidsh_rl.model import Sample
from minidsh_rl.priced_backend import PricedBackend
from minidsh_rl.recording import FileRecordingSink
from minidsh_rl.weight_probe import weight_delta


class FinancialBackend(PricedBackend):
    """Bind each known sample to exactly one durable financial operation."""

    directory: Path
    call_index: int = 0

    async def sample(self, prompt: tuple[int, ...], limit: int, seed: int) -> Sample:
        before = _State.model_validate_json(self.budget.path.read_bytes())
        started = datetime.now(UTC).isoformat()
        result = await super().sample(prompt, limit, seed)
        after = _State.model_validate_json(self.budget.path.read_bytes())
        new = set(after.charges) - set(before.charges)
        if len(new) != 1:
            raise ValueError("sample finance binding ambiguous")
        operation = new.pop()
        charge = after.charges[operation]
        if charge.status != "estimated":
            raise ValueError("sample not financially settled")
        await FileRecordingSink(self.directory / "financial-calls").append(
            {
                "call_index": self.call_index,
                "operation": operation,
                "seed": seed,
                "started_at_utc": started,
                "returned_at_utc": datetime.now(UTC).isoformat(),
                "input_tokens": len(prompt),
                "output_tokens": len(result.turn.completion),
                "estimate_cny": str(charge.amount),
            }
        )
        self.call_index += 1
        return result


class CurveSDKRuntime(LearningSDKRuntime):
    async def sample(
        self, case: LearningCase, row: CurveRow, before: Checkpoint, output: Path
    ) -> LearningRollout:
        self._pair(before)
        if (
            self.tokenizer is None
            or before != self.current
            or case.dataset_version not in {"curve_dataset_v2", "curve_dataset_v4"}
        ):
            raise ValueError("curve sampling identity mismatch")
        backend = FinancialBackend(
            self.samplers[before.policy.policy_id],
            self.tokenizer,
            self.budget,
            before.policy.policy_id,
        )
        backend.directory = output
        return await run_learning_rollout(
            case,
            backend,
            policy=before.policy,
            run_id=row.run_id,
            seed=row.seed,
            output=output,
            host=self.host,
            image=self.image,
        )

    async def reconnect(self, checkpoint: Checkpoint, attempt: int) -> None:
        if self.client is not None or not 1 <= attempt <= MAX_RESUMES:
            raise ValueError("invalid curve reconnect")
        self._pair(checkpoint)
        initial = Checkpoint.model_validate_json((self.root / "initial.json").read_bytes())
        self._pair(initial)
        folder = self.root / "resumes" / f"attempt_{attempt:03}"
        folder.mkdir(parents=True, exist_ok=False)

        def connect() -> None:
            self.client = self.service.create_training_client_from_state_with_optimizer(
                checkpoint.optimizer_uri
            )
            self._check_tokenizer()
            for item in (initial, checkpoint):
                self.samplers[item.policy.policy_id] = self._connect(item.sampler_uri)

        await asyncio.to_thread(self._trainer(folder).charge, "restore", Decimal(".01"), connect)
        atomic_record(folder / "restored.json", checkpoint, create=True)
        self.current = checkpoint


def initial_audit(
    runtime: LearningSDKRuntime, checkpoint: Checkpoint, output: Path, baseline: Path
) -> Decimal:
    """Callback executes on a worker thread after full resource admission."""
    if runtime.client is None:
        raise ValueError("missing initial client")
    client = runtime.client
    candidate = output / "initial.archive"
    runtime._trainer(output).charge(
        "curve_initial_audit",
        Decimal(".02"),
        lambda: asyncio.run(
            download_owned(client.training_run_cuid, checkpoint.sampler_uri, candidate)
        ),
    )
    metrics = validate_initial_adapter(baseline, candidate)
    (output / "curve-initial-audit.json").write_text(
        json.dumps(
            {
                "checkpoint": checkpoint.model_dump(mode="json"),
                "metrics": metrics,
                "archive_sha256": digest(candidate),
                "baseline_sha256": digest(baseline),
            },
            indent=2,
        )
    )
    return Decimal(".02")


async def final_audit(runtime: CurveSDKRuntime, checkpoint: Checkpoint) -> None:
    pair = runtime._pair(checkpoint)
    folder = runtime.root / "final-audit"
    folder.mkdir(exist_ok=False)
    archive = folder / "final.archive"
    await asyncio.to_thread(
        runtime._trainer(folder).charge,
        "curve_final_audit",
        Decimal(".02"),
        lambda: asyncio.run(
            download_owned(pair.training_run_cuid, checkpoint.sampler_uri, archive)
        ),
    )
    initial = runtime.root / "initial-sdk/initial.archive"
    delta = weight_delta(initial, archive)
    if delta <= 0:
        raise ValueError("training did not change adapter")
    (folder / "result.json").write_text(
        json.dumps(
            {
                "parameter_delta_l2": delta,
                "initial_sha256": digest(initial),
                "final_sha256": digest(archive),
            }
        )
    )
