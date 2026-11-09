"""Provider bridge for function-level GSPO; the core AgentLoop stays unchanged."""

import asyncio
import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Literal

from minidsh_rl.curve_sdk import CurveSDKRuntime, initial_audit
from minidsh_rl.gspo import GSPOBatch
from minidsh_rl.gspo_study import CONFIG, StudyRow, batch_digest
from minidsh_rl.gspo_trainer import GSPOTrainer
from minidsh_rl.learning_execution import Checkpoint, GroupRecord, UpdateReceipt
from minidsh_rl.learning_schedule import atomic_record, digest
from minidsh_rl.priced_backend import PricedBackend
from minidsh_rl.rollout_v2 import Rollout, run_rollout
from minidsh_rl.tasks_v2 import expansion_tasks
from minidsh_rl.train_run import _verify_tokenizer_identity
from minidsh_rl.trainer_v2 import TrainerV2
from minidsh_rl.transaction import UpdateJournal
from minidsh_rl.weight_probe import weight_delta


class GspoStudyRuntime(CurveSDKRuntime):
    async def initialize(self, directory: Path) -> Checkpoint:
        if directory != self.root or self.client is not None:
            raise ValueError("invalid initial runtime")
        folder = directory / "initial-sdk"
        folder.mkdir(exist_ok=False)
        self.client = await asyncio.to_thread(
            self._trainer(folder).charge,
            "create_client",
            Decimal(".01"),
            lambda: self.service.create_lora_training_client(
                base_model="Qwen/Qwen3.5-4B",
                rank=16,
                seed=17,
                train_mlp=True,
                train_attn=True,
                train_unembed=False,
            ),
        )
        client = self.client
        if client is None:
            raise ValueError("training client registration missing")
        (folder / "registered-client.json").write_text(
            json.dumps({"training_run_cuid": client.training_run_cuid})
        )
        await asyncio.to_thread(self._check_tokenizer)
        checkpoint = await asyncio.to_thread(self._save_pair, folder, "initial")
        reference = Path(json.loads((directory / "reference.json").read_text())["archive"])
        await asyncio.to_thread(initial_audit, self, checkpoint, folder, reference)
        delta = await asyncio.to_thread(weight_delta, reference, folder / "initial.archive")
        if not math.isfinite(delta) or delta != 0:
            raise ValueError("initial parameters differ from historical fixed seed baseline")
        (folder / "initial-equivalence.json").write_text(
            json.dumps({"adapter_delta_l2": delta, "source_archive_sha256": digest(reference)})
        )
        atomic_record(directory / "initial.json", checkpoint, create=True)
        self.current = checkpoint
        return checkpoint

    async def sample_function(
        self, row: StudyRow, before: Checkpoint, output: Path, reserve: Decimal
    ) -> Rollout:
        self._pair(before)
        initial = Checkpoint.model_validate_json((self.root / "initial.json").read_bytes())
        expected = initial if row.policy == "base" else self.current
        if before != expected or self.tokenizer is None:
            raise ValueError("paired policy mismatch")
        backend = PricedBackend(
            self.samplers[before.policy.policy_id],
            self.tokenizer,
            self.budget,
            before.policy.policy_id,
            evaluation_reserve=reserve,
        )
        _verify_tokenizer_identity(backend.identity())
        case = next(c for c in expansion_tasks() if c.case_id == row.case_id)
        return await run_rollout(
            case,
            backend,
            policy_id=before.policy.policy_id,
            run_id=row.run_id,
            seed=row.seed,
            output=output,
            host=self.host,
            image=self.image,
            instruction_suffix="",
        )

    async def update_gspo(
        self, key: str, batch: GSPOBatch, before: Checkpoint, output: Path, reserve: Decimal
    ) -> None:
        self._pair(before)
        if before != self.current or self.client is None:
            raise ValueError("training policy changed")
        record = GroupRecord.model_validate_json((output / "group.json").read_bytes())
        if (
            record.before != before
            or record.datum_sha256 != batch_digest(batch)
            or record.has_signal != batch.has_signal
        ):
            raise ValueError("prepared GSPO datum changed")
        slot = int(key.rsplit("_", 1)[-1])
        trainer = TrainerV2(self.client, self.budget, output, "sum", reserve)
        result = await asyncio.to_thread(GSPOTrainer(trainer, CONFIG).update, slot, batch)
        if result["datum_hash"] != record.datum_sha256:
            raise ValueError("GSPO journal digest mismatch")
        journal_path = output / f"update_{slot:03}.json"
        if result["updated"]:
            after = await asyncio.to_thread(self._save_pair, output, key)
            journal = UpdateJournal.load(journal_path)
            journal.advance("checkpointed", checkpoint=after.optimizer_uri)
        else:
            after = before
        atomic_record(
            output / "receipt.json",
            UpdateReceipt(
                group_sha256=digest(output / "group.json"),
                before=before,
                after=after,
                updated=bool(result["updated"]),
                journal_sha256=digest(journal_path),
            ),
            create=True,
        )
        self.current = after


class ProviderRuntime:
    """Narrow bridge avoids overriding unrelated LearningCase methods."""

    execution_kind: Literal["provider", "contract_fixture"] = "provider"

    def __init__(self, runtime: GspoStudyRuntime) -> None:
        self.runtime = runtime

    async def initialize(self, root: Path) -> Checkpoint:
        return await self.runtime.initialize(root)

    async def reconnect(self, before: Checkpoint, attempt: int) -> None:
        await self.runtime.reconnect(before, attempt)

    async def sample(
        self, row: StudyRow, before: Checkpoint, output: Path, reserve: Decimal
    ) -> Rollout:
        return await self.runtime.sample_function(row, before, output, reserve)

    async def update(
        self, key: str, batch: GSPOBatch, before: Checkpoint, output: Path, reserve: Decimal
    ) -> None:
        await self.runtime.update_gspo(key, batch, before, output, reserve)
