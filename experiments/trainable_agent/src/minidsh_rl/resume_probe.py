"""Compare an uninterrupted Adam update with restoration of its real Train state."""

import argparse
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import TypeVar

import pytrio as trio  # type: ignore[import-untyped]

from minidsh_rl.budget import BudgetStore, cost_upper_bound
from minidsh_rl.contracts import Turn
from minidsh_rl.datum import build_batch
from minidsh_rl.trainer import CLIP, normalized_batch
from minidsh_rl.weight_probe import weight_delta

_T = TypeVar("_T")
RELATIVE_TOLERANCE = 0.03


def main() -> None:  # noqa: PLR0915 - explicit recovery gate sequence
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--turn", type=Path, required=True)
    parser.add_argument("--initial-state", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    args = parser.parse_args()
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=False)
    raw = json.loads(args.turn.read_text())
    turn = Turn(
        tuple(raw["prompt_token_ids"]),
        tuple(raw["completion_token_ids"]),
        tuple(raw["old_logprobs"]),
    )
    batch = normalized_batch(build_batch(((turn,),), (1.0,)), "sample_mean")
    with BudgetStore(args.budget, Decimal("50")) as budget:

        def charge(name: str, maximum: Decimal, call: Callable[[], _T]) -> _T:
            operation = output.name + "_" + name
            if budget.ledger.available - maximum < Decimal("15"):
                raise ValueError("evaluation reserve would be consumed")
            budget.reserve(operation, maximum)
            (output / "last_operation.json").write_text(
                json.dumps({"operation": operation, "state": "submitted"})
            )
            result = call()
            budget.settle(operation, maximum)
            (output / "last_operation.json").write_text(
                json.dumps({"operation": operation, "state": "confirmed"})
            )
            return result

        service = trio.ServiceClient()
        rest = service.create_rest_client()
        source = str(json.loads(args.initial_state.read_text())["path"])
        trainer = charge(
            "initial_restore",
            Decimal(".01"),
            lambda: service.create_training_client_from_state_with_optimizer(source),
        )

        def update(label: str) -> None:
            response = charge(
                label + "_fb",
                cost_upper_bound(0, 0, batch.padded_tokens),
                lambda: trainer.forward_backward(
                    list(batch.datums), loss_fn="ppo", loss_fn_config=CLIP
                ).result(),
            )
            (output / (label + "_fb.json")).write_text(response.model_dump_json(indent=2))
            optimized = charge(
                label + "_optim",
                Decimal(".01"),
                lambda: trainer.optim_step(
                    trio.AdamParams(
                        learning_rate=1e-5, beta1=0.9, beta2=0.95, weight_decay=0, grad_clip_norm=1
                    )
                ).result(),
            )
            (output / (label + "_optim.json")).write_text(optimized.model_dump_json(indent=2))

        def export(label: str) -> Path:
            weights = charge(
                label + "_export",
                Decimal(".10"),
                lambda: trainer.save_weights_for_sampler(label).result(),
            )
            (output / (label + "_weights.json")).write_text(weights.model_dump_json(indent=2))
            checkpoints = rest.list_checkpoints(trainer.training_run_cuid).result()
            cid = next(c.checkpoint_id for c in checkpoints.checkpoints if c.path == weights.path)
            path = output / (label + ".archive")
            rest.download_checkpoint(cid, path, resume=False).result()
            return path

        update("warmup")
        state = charge(
            "save_state", Decimal(".10"), lambda: trainer.save_state("after_warmup").result()
        )
        (output / "state.json").write_text(state.model_dump_json(indent=2))
        baseline = export("before")
        update("original")
        original = export("original")
        trainer = charge(
            "restore_with_optimizer",
            Decimal(".01"),
            lambda: service.create_training_client_from_state_with_optimizer(state.path),
        )
        update("replay")
        replay = export("replay")
        difference = weight_delta(original, replay)
        update_size = weight_delta(baseline, original)
        relative = difference / update_size if update_size else float("inf")
        result = {
            "passed": relative <= RELATIVE_TOLERANCE,
            "difference_norm": difference,
            "update_norm": update_size,
            "relative_difference": relative,
            "threshold": RELATIVE_TOLERANCE,
            "conservative_spend_cny": str(budget.ledger.spent),
        }
        (output / "result.json").write_text(json.dumps(result, indent=2))
        if not result["passed"]:
            raise ValueError("optimizer checkpoint recovery differs from uninterrupted update")


if __name__ == "__main__":
    main()
