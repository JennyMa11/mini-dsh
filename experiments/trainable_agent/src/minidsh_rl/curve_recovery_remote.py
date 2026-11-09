"""Read-only owner/terminal verification before a separately registered recovery."""

from pytrio.config.config import auth_state  # type: ignore[import-untyped]
from pytrio.transport.control.http_client import ControlHTTPClient  # type: ignore[import-untyped]

from minidsh_rl.curve_recovery import LORA_RANK, TRAIN_SEED

MODEL = "Qwen/Qwen3.5-4B"


async def terminal_metadata(uid: str, source: str) -> dict[str, object]:
    client = ControlHTTPClient(api_key=auth_state.api_key)
    try:
        auth = await client.verify_api_key()
        run = await client.get_training_run(uid, access_scope="owned")
        listing = await client.list_training_run_checkpoints(uid)
        if (
            run.workspace_cuid != auth.workspace_cuid
            or run.status != "ENDED"
            or run.source_checkpoint_uri != source
            or not run.restore_optimizer
            or run.base_model != MODEL
            or run.lora_rank != LORA_RANK
            or run.seed != TRAIN_SEED
            or run.corrupted
            or listing.checkpoints
        ):
            raise ValueError("old initialization is not owned, terminal and checkpoint-free")
        return {
            "training_run_cuid": uid,
            "owned": True,
            "status": run.status,
            "checkpoint_count": len(listing.checkpoints),
            "restore_optimizer": True,
            "source_optimizer": source,
            "model": MODEL,
            "lora_rank": run.lora_rank,
            "seed": run.seed,
        }
    finally:
        await client.aclose()
