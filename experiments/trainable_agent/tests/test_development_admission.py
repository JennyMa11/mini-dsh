"""No factory before full funding; borrowed policy ownership checked without training."""

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from minidsh_rl import development_run
from minidsh_rl.cost_store import CostStore
from minidsh_rl.development_schedule import make_development_plan
from minidsh_rl.learning_execution import Checkpoint
from minidsh_rl.learning_rollout import PolicyBinding
from minidsh_rl.learning_run import ExecutionConfig
from minidsh_rl.learning_sdk import SavedPair, SavedWeight


def pair_fixture() -> SavedPair:
    return SavedPair(
        training_run_cuid="run_unit",
        sampling_run_cuid="sample_unit",
        tokenizer_sha256="b" * 64,
        optimizer=SavedWeight(
            path="trio://run_unit/state", model="Qwen/Qwen3.5-4B", size=1, type="save-checkpoint"
        ),
        sampler=SavedWeight(
            path="trio://run_unit/sampler", model="Qwen/Qwen3.5-4B", size=1, type="save-weights"
        ),
    )


@pytest.mark.parametrize("workspace", ["owner", "other"])
async def test_owned_sampler_metadata_and_no_training_api(
    monkeypatch: pytest.MonkeyPatch, workspace: str
) -> None:
    pair = pair_fixture()
    closed = []

    class Client:
        def __init__(self, *, api_key: str) -> None:
            assert api_key == "unit-key"

        async def verify_api_key(self) -> SimpleNamespace:
            return SimpleNamespace(workspace_cuid="owner")

        async def get_training_run(self, uid: str, *, access_scope: str) -> SimpleNamespace:
            assert uid == pair.training_run_cuid
            assert access_scope == "owned"
            return SimpleNamespace(workspace_cuid=workspace, base_model="Qwen/Qwen3.5-4B")

        async def list_training_run_checkpoints(self, uid: str) -> SimpleNamespace:
            assert uid == pair.training_run_cuid
            return SimpleNamespace(
                checkpoints=[
                    SimpleNamespace(
                        checkpoint_uri=pair.sampler.path,
                        status="READY",
                        training_run_cuid=uid,
                        workspace_cuid="owner",
                        path=f"/owner/{uid}/weights",
                        checkpoint_cuid="cp_unit",
                    )
                ]
            )

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(development_run, "auth_state", SimpleNamespace(api_key="unit-key"))
    monkeypatch.setattr(development_run, "ControlHTTPClient", Client)
    if workspace == "owner":
        result = await development_run.owned_metadata(pair)
        assert result["new_training_clients"] == 0
        assert result["source_uri"] == pair.sampler.path
    else:
        with pytest.raises(ValueError, match="ownership"):
            await development_run.owned_metadata(pair)
    assert closed == [True]


async def test_insufficient_funding_does_not_connect_any_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "legacy.json"
    legacy.write_text('{"schema_version":1,"limit":"150","spent":"0","pending":{},"settled":[]}')
    quote = tmp_path / "quote.json"
    quote.write_text("{}")

    class ZeroAvailable(CostStore):
        @property
        def available(self) -> Decimal:
            return Decimal("0")

    budget = ZeroAvailable(
        tmp_path / "budget.json",
        legacy=legacy,
        quote=quote,
        limit=Decimal("150"),
        uncertainty=Decimal("0"),
    )
    plan = make_development_plan().model_copy(
        update={"finance_directory": str(tmp_path), "start_committed": Decimal("0")}
    )
    checkpoint = Checkpoint(
        execution_kind="provider",
        sampler_uri="trio://unit/sampler",
        optimizer_uri="trio://unit/state",
        policy=PolicyBinding(
            kind="provider", policy_id="unit", snapshot_sha256="a" * 64, tokenizer_sha256="b" * 64
        ),
    )
    monkeypatch.setattr(
        development_run,
        "load_development",
        lambda _root: (plan, cast(ExecutionConfig, object()), checkpoint, pair_fixture()),
    )
    monkeypatch.setattr(development_run, "CurveBudget", lambda _root: budget)

    async def forbidden(_root: Path) -> None:
        raise AssertionError("provider preflight must not happen")

    monkeypatch.setattr(development_run, "service_evidence", forbidden)
    with pytest.raises(ValueError, match="complete"):
        await development_run.run_development(tmp_path)
    assert not (tmp_path / "started.json").exists()
