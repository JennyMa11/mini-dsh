"""Restore the same initial optimizer, then use the established measured GRPO kernel."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from minidsh_rl.curve_schedule import CurvePlan
from minidsh_rl.curve_sdk import CurveSDKRuntime, initial_audit
from minidsh_rl.curve_weights import download_owned
from minidsh_rl.empty_optimizer import compare_empty_optimizers
from minidsh_rl.learning_execution import Checkpoint
from minidsh_rl.learning_schedule import atomic_record, digest
from minidsh_rl.weight_probe import weight_delta


class LearningCurveRuntime(CurveSDKRuntime):
    async def initialize(self, directory: Path) -> Checkpoint:
        if directory != self.root or self.client is not None:
            raise ValueError("invalid restored initialization")
        source = Checkpoint.model_validate_json((directory / "source-checkpoint.json").read_bytes())
        folder = directory / "initial-sdk"
        folder.mkdir(exist_ok=False)
        plan = CurvePlan.model_validate_json((directory / "plan.json").read_bytes())

        def create() -> object:
            if plan.initialization == "verified_fresh_seed17":
                return self.service.create_lora_training_client(
                    base_model="Qwen/Qwen3.5-4B",
                    rank=16,
                    seed=17,
                    train_mlp=True,
                    train_attn=True,
                    train_unembed=False,
                )
            return self.service.create_training_client_from_state_with_optimizer(
                source.optimizer_uri
            )

        self.client = await asyncio.to_thread(
            self._trainer(folder).charge,
            "restore_initial_optimizer",
            Decimal(".01"),
            create,
        )
        client = self.client
        if client is None:
            raise ValueError("restored client was not registered")
        (folder / "registered-client.json").write_text(
            json.dumps(
                {
                    "training_run_cuid": client.training_run_cuid,
                    "source_optimizer": source.optimizer_uri,
                }
            )
        )
        await asyncio.to_thread(self._check_tokenizer)
        checkpoint = await asyncio.to_thread(self._save_pair, folder, "initial")
        reference = Path(json.loads((directory / "initial-source.json").read_text())["archive"])
        preparation = await asyncio.to_thread(initial_audit, self, checkpoint, folder, reference)
        delta = await asyncio.to_thread(weight_delta, reference, folder / "initial.archive")
        if delta != 0:
            raise ValueError("initial adapter does not equal admitted development policy")
        if plan.initialization == "verified_fresh_seed17":
            await asyncio.to_thread(
                self._trainer(folder).charge,
                "empty_optimizer_audit",
                Decimal(".02"),
                lambda: asyncio.run(
                    download_owned(
                        client.training_run_cuid,
                        checkpoint.optimizer_uri,
                        folder / "initial-optimizer.archive",
                    )
                ),
            )
            await asyncio.to_thread(
                compare_empty_optimizers,
                directory / "source-optimizer.archive",
                folder / "initial-optimizer.archive",
            )
        (folder / "restored-equivalence.json").write_text(
            json.dumps(
                {
                    "adapter_delta_l2": delta,
                    "source_archive_sha256": digest(reference),
                    "restored_archive_sha256": digest(folder / "initial.archive"),
                    "source_optimizer": source.optimizer_uri,
                    "initialization": plan.initialization,
                    "empty_optimizer_equivalence": plan.initialization == "verified_fresh_seed17",
                }
            )
        )
        checkpoint = Checkpoint.model_validate(
            checkpoint.model_dump() | {"preparation_spent": preparation}
        )
        atomic_record(directory / "initial.json", checkpoint, create=True)
        self.current = checkpoint
        return checkpoint
