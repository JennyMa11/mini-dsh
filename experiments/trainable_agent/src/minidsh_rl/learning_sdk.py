"""Concrete SDK wiring with explicit dependencies; no session or RPC at import/init."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol, cast

from pydantic import Field
from pytrio import ServiceClient  # type: ignore[import-untyped]
from pytrio.client.training import TrainingClient  # type: ignore[import-untyped]

from minidsh.infrastructure.process import ProcessRunner
from minidsh_rl.backend_v2 import Tokenizer
from minidsh_rl.cost_store import CostStore, _State
from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.learning_execution import (
    Checkpoint,
    ExecutionState,
    group_datum_hash,
    write_update_receipt,
)
from minidsh_rl.learning_feedback import verify_feedback
from minidsh_rl.learning_rollout import (
    LearningGroup,
    LearningRollout,
    PolicyBinding,
    run_learning_rollout,
)
from minidsh_rl.learning_schedule import (
    MAX_RESUMES,
    STAGED_MAX_RESUMES,
    FrozenRecord,
    QueueRow,
    atomic_record,
)
from minidsh_rl.minirepo_verification import IMAGE
from minidsh_rl.priced_backend import PricedBackend
from minidsh_rl.trainer_v2 import TrainerV2
from minidsh_rl.transaction import UpdateJournal

MODEL = "Qwen/Qwen3.5-4B"


def tokenizer_pin(tokenizer: Tokenizer) -> str:
    """Bind vocabulary, template and fixed renderer, excluding mutable policy IDs."""
    identity = {
        "vocabulary_hash": hashlib.sha256(
            json.dumps(tokenizer.get_vocab(), sort_keys=True).encode()
        ).hexdigest(),
        "template_hash": hashlib.sha256(tokenizer.chat_template.encode()).hexdigest(),
        "enable_thinking": "false",
        "rendering_version": "qwen35_xml_feedback_v2",
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


class SavedWeight(FrozenRecord):
    path: str = Field(pattern=r"^trio://[^\s]+$")
    model: Literal["Qwen/Qwen3.5-4B"]
    size: int = Field(gt=0, strict=True)
    type: Literal["save-checkpoint", "save-weights"]


class SavedPair(FrozenRecord):
    training_run_cuid: str = Field(min_length=1)
    sampling_run_cuid: str = Field(min_length=1)
    optimizer: SavedWeight
    sampler: SavedWeight
    tokenizer_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class _Sampler(Protocol):
    sampling_run_cuid: str
    model_id: str


def _binding(pair: SavedPair) -> PolicyBinding:
    summary = hashlib.sha256(pair.model_dump_json().encode()).hexdigest()
    return PolicyBinding(
        kind="provider",
        policy_id="policy_" + summary[:24],
        snapshot_sha256=summary,
        tokenizer_sha256=pair.tokenizer_sha256,
    )


class LeasedResources:
    """ResourcePort over a caller-owned live CostStore; never acquires a new budget."""

    def __init__(self, budget: CostStore) -> None:
        self.budget = budget

    @property
    def available(self) -> Decimal:
        return self.budget.available

    @property
    def committed(self) -> Decimal:
        return self.budget.committed

    @property
    def has_pending(self) -> bool:
        # Reading available first checks that the spending lease is still live.
        _ = self.budget.available
        state = _State.model_validate_json(self.budget.path.read_bytes())
        return any(charge.status == "pending" for charge in state.charges.values())


class LearningSDKRuntime:
    """RuntimePort implementation; the controller must admit the entire stage first."""

    execution_kind: Literal["provider"] = "provider"

    def __init__(  # noqa: PLR0913 - dependencies are explicit at the SDK boundary
        self,
        service: object,
        budget: CostStore,
        root: Path,
        host: ProcessRunner,
        expected_tokenizer: str,
        *,
        image: str = IMAGE,
        initial_check: Callable[[LearningSDKRuntime, Checkpoint, Path], Decimal] | None = None,
        resume_allowance: int = MAX_RESUMES,
    ) -> None:
        if re.fullmatch(r"[a-f0-9]{64}", expected_tokenizer) is None:
            raise ValueError("invalid tokenizer pin")
        if type(resume_allowance) is not int or resume_allowance not in {
            MAX_RESUMES,
            STAGED_MAX_RESUMES,
        }:
            raise ValueError("invalid resume allowance")
        self.service = cast(ServiceClient, service)
        self.budget, self.root, self.host, self.image = budget, root, host, image
        self.expected_tokenizer = expected_tokenizer
        self.client: TrainingClient | None = None
        self.tokenizer: Tokenizer | None = None
        self.current: Checkpoint | None = None
        self.samplers: dict[str, object] = {}
        self.initial_check = initial_check
        self.resume_allowance = resume_allowance

    def _trainer(self, directory: Path) -> TrainerV2:
        return TrainerV2(self.client, self.budget, directory, "sample_mean")

    def _check_tokenizer(self) -> None:
        if self.client is None or self.client.model_id != MODEL:
            raise ValueError("training model identity mismatch")
        tokenizer = cast(Tokenizer, self.client.get_tokenizer())
        if tokenizer_pin(tokenizer) != self.expected_tokenizer:
            raise ValueError("tokenizer identity mismatch")
        self.tokenizer = tokenizer

    def _connect(self, uri: str) -> object:
        sampler = self.service.create_sampling_client(base_model=MODEL, model_path=uri)
        if sampler.model_id != MODEL or not sampler.sampling_run_cuid:
            raise ValueError("sampling model identity mismatch")
        return sampler

    def _pair(self, checkpoint: Checkpoint) -> SavedPair:
        if checkpoint.execution_kind != "provider" or checkpoint.policy.kind != "provider":
            raise ValueError("provider checkpoint required")
        if re.fullmatch(r"policy_[a-f0-9]{24}", checkpoint.policy.policy_id) is None:
            raise ValueError("invalid saved policy ID")
        path = self.root / "policies" / (checkpoint.policy.policy_id + ".json")
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("saved policy path rejected")
        pair = SavedPair.model_validate_json(path.read_bytes())
        if (
            _binding(pair) != checkpoint.policy
            or pair.optimizer.path != checkpoint.optimizer_uri
            or pair.sampler.path != checkpoint.sampler_uri
            or pair.optimizer.type != "save-checkpoint"
            or pair.sampler.type != "save-weights"
            or pair.tokenizer_sha256 != self.expected_tokenizer
        ):
            raise ValueError("saved policy binding mismatch")
        return pair

    def _save_pair(self, directory: Path, label: str) -> Checkpoint:
        if self.client is None or self.tokenizer is None:
            raise ValueError("runtime not initialized")
        trainer = self._trainer(directory)
        state_name = "state_" + label
        optimizer_uri = trainer.checkpoint(state_name)
        optimizer = SavedWeight.model_validate_json(
            (directory / (state_name + ".json")).read_bytes()
        )
        if optimizer.type != "save-checkpoint" or optimizer.path != optimizer_uri:
            raise ValueError("optimizer save receipt mismatch")
        client = self.client

        def save_sampler() -> tuple[SavedWeight, object]:
            response = client.save_weights_for_sampler("sampler_" + label).result()
            saved = SavedWeight.model_validate_json(response.model_dump_json())
            if saved.type != "save-weights":
                raise ValueError("sampler save receipt mismatch")
            atomic_record(directory / "sampler-save.json", saved, create=True)
            return saved, self._connect(saved.path)

        saved, sampler = trainer.charge("sampler_" + label, Decimal(".10"), save_sampler)
        pair = SavedPair(
            training_run_cuid=self.client.training_run_cuid,
            sampling_run_cuid=cast(_Sampler, sampler).sampling_run_cuid,
            optimizer=optimizer,
            sampler=saved,
            tokenizer_sha256=self.expected_tokenizer,
        )
        policy = _binding(pair)
        folder = self.root / "policies"
        folder.mkdir(exist_ok=True)
        atomic_record(folder / (policy.policy_id + ".json"), pair, create=True)
        self.samplers[policy.policy_id] = sampler
        return Checkpoint(
            execution_kind="provider",
            sampler_uri=saved.path,
            optimizer_uri=optimizer.path,
            policy=policy,
        )

    async def initialize(self, directory: Path) -> Checkpoint:
        if directory != self.root or self.client is not None:
            raise ValueError("invalid runtime initialization")
        folder = directory / "initial-sdk"
        folder.mkdir(exist_ok=False)
        self.client = await asyncio.to_thread(
            self._trainer(folder).charge,
            "create_client",
            Decimal(".01"),
            lambda: self.service.create_lora_training_client(
                base_model=MODEL,
                rank=16,
                seed=17,
                train_mlp=True,
                train_attn=True,
                train_unembed=False,
            ),
        )
        await asyncio.to_thread(self._check_tokenizer)
        checkpoint = await asyncio.to_thread(self._save_pair, folder, "initial")
        if self.initial_check is not None:
            extra = await asyncio.to_thread(self.initial_check, self, checkpoint, folder)
            checkpoint = Checkpoint.model_validate(
                checkpoint.model_dump() | {"preparation_spent": extra}
            )
        atomic_record(directory / "initial.json", checkpoint, create=True)
        self.current = checkpoint
        return checkpoint

    async def restore(self, before: Checkpoint, directory: Path) -> None:
        if directory != self.root or self.client is not None:
            raise ValueError("invalid runtime restore")
        initial = Checkpoint.model_validate_json((directory / "initial.json").read_bytes())
        self._pair(before)
        self._pair(initial)
        state = ExecutionState.model_validate_json((directory / "state.json").read_bytes())
        if (
            state.pending_kind != "resume"
            or state.current != before
            or not 1 <= state.resumes <= self.resume_allowance
        ):
            raise ValueError("restore was not admitted by controller")
        folder = directory / "resumes" / f"attempt_{state.resumes:03}"
        folder.mkdir(parents=True, exist_ok=False)

        def reconnect() -> None:
            self.client = self.service.create_training_client_from_state_with_optimizer(
                before.optimizer_uri
            )
            self._check_tokenizer()
            for checkpoint in (before, initial):
                if checkpoint.policy.policy_id not in self.samplers:
                    self.samplers[checkpoint.policy.policy_id] = self._connect(
                        checkpoint.sampler_uri
                    )

        await asyncio.to_thread(self._trainer(folder).charge, "restore", Decimal(".01"), reconnect)
        atomic_record(folder / "restored.json", before, create=True)
        self.current = before

    async def rollout(
        self, case: LearningCase, row: QueueRow, before: Checkpoint, directory: Path
    ) -> LearningRollout:
        self._pair(before)
        initial = Checkpoint.model_validate_json((self.root / "initial.json").read_bytes())
        expected = initial if row.policy == "base" else self.current
        if (
            before != expected
            or self.tokenizer is None
            or tokenizer_pin(self.tokenizer) != self.expected_tokenizer
            or row.case_id != case.task.case_id
        ):
            raise ValueError("rollout policy/case mismatch")
        backend = PricedBackend(
            self.samplers[before.policy.policy_id],
            self.tokenizer,
            self.budget,
            before.policy.policy_id,
        )
        return await run_learning_rollout(
            case,
            backend,
            policy=before.policy,
            run_id=row.run_id,
            seed=row.seed,
            output=directory,
            host=self.host,
            image=self.image,
        )

    async def feedback(self, rollout: LearningRollout, directory: Path) -> bool:
        return await verify_feedback(
            rollout, self.root / "rollouts" / rollout.run_id, directory, self.host, self.image
        )

    async def update(
        self, key: str, group: LearningGroup, before: Checkpoint, directory: Path
    ) -> None:
        self._pair(before)
        if before != self.current or self.client is None:
            raise ValueError("update policy mismatch")
        slot = int(key.rsplit("_", maxsplit=1)[-1])
        path = directory / f"update_{slot:03}.json"
        if group.batch is None:
            journal = UpdateJournal.create(path, group_datum_hash(group))
            journal.advance("skipped")
            updated = False
        else:
            result = await asyncio.to_thread(self._trainer(directory).update, slot, group.batch)
            updated = result["updated"] is True
            journal = UpdateJournal.load(path)
        after = before
        if updated:
            after = await asyncio.to_thread(self._save_pair, directory, f"step_{slot:03}")
            journal.advance("checkpointed", checkpoint=after.optimizer_uri)
        write_update_receipt(directory, key, group, before, after, updated=updated)
        self.current = after
