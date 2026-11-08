"""Measure actual LoRA deltas when backend optimizer metrics are unavailable."""

import argparse
import json
import math
import tarfile
import zipfile
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import TypedDict, TypeVar, cast

import numpy as np
import pytrio as trio  # type: ignore[import-untyped]
from safetensors import deserialize

from minidsh_rl.budget import BudgetStore, cost_upper_bound
from minidsh_rl.contracts import Turn
from minidsh_rl.datum import build_batch

_T = TypeVar("_T")
CLIP = {"clip_low_threshold": 0.8, "clip_high_threshold": 1.2}
RATIO_TOLERANCE = 0.03
LOGPROB_TOLERANCE = 0.001


def tensor_blobs(path: Path) -> dict[str, bytes]:
    """Read safetensors without extracting arbitrary archive paths."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return {
                name: archive.read(name)
                for name in archive.namelist()
                if name.endswith(".safetensors")
            }
    if tarfile.is_tarfile(path):
        result = {}
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                if member.name.endswith(".safetensors") and member.isfile():
                    stream = archive.extractfile(member)
                    if stream is not None:
                        result[member.name] = stream.read()
        return result
    return {"adapter.safetensors": path.read_bytes()}


class _Tensor(TypedDict):
    dtype: str
    shape: list[int]
    data: bytearray


_DESERIALIZE = cast(Callable[[bytes], list[tuple[str, _Tensor]]], deserialize)


def tensor_arrays(blob: bytes) -> dict[str, np.ndarray]:
    """Use Rust validation, then convert BF16 bits without requiring Torch."""
    result = {}
    for name, tensor in _DESERIALIZE(blob):
        if tensor["dtype"] == "BF16":
            bits = np.frombuffer(tensor["data"], dtype="<u2").astype(np.uint32) << 16
            array = bits.view(np.float32)
        else:
            dtype = {"F32": "<f4", "F16": "<f2", "F64": "<f8"}.get(tensor["dtype"])
            if dtype is None:
                raise ValueError("unsupported adapter tensor dtype")
            array = np.frombuffer(tensor["data"], dtype=dtype)
        result[name] = array.reshape(tensor["shape"])
    return result


def weight_delta(before: Path, after: Path) -> float:
    """Euclidean adapter update size; both exports must cover identical tensors."""
    left, right = tensor_blobs(before), tensor_blobs(after)
    if set(left) != set(right) or not left:
        raise ValueError("incompatible adapter exports")
    total = 0.0
    for name in left:
        a, b = tensor_arrays(left[name]), tensor_arrays(right[name])
        if set(a) != set(b):
            raise ValueError("adapter tensor names changed")
        for key in a:
            delta = np.asarray(b[key], dtype=np.float64) - np.asarray(a[key], dtype=np.float64)
            total += float(np.square(delta).sum())
    return math.sqrt(total)


def main() -> None:  # noqa: PLR0915 - explicit numerical stages
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--turn", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--initial-state", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--continue-probe", action="store_true")
    args = parser.parse_args()
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=args.continue_probe)
    if args.continue_probe:
        last = json.loads((output / "last_operation.json").read_text())
        if last["state"] != "confirmed":
            raise ValueError("cannot continue an unknown RPC")
    raw = json.loads(args.turn.read_text())
    turn = Turn(
        tuple(raw["prompt_token_ids"]),
        tuple(raw["completion_token_ids"]),
        tuple(raw["old_logprobs"]),
    )
    cut = len(turn.completion) // 2
    split = (
        Turn(turn.prompt, turn.completion[:cut], turn.logprobs[:cut]),
        Turn(turn.prompt + turn.completion[:cut], turn.completion[cut:], turn.logprobs[cut:]),
    )
    batches = {
        "zero": build_batch(((turn,),), (0.0,)),
        "single": build_batch(((turn,),), (1.0,)),
        "duplicate": build_batch(((turn,), (turn,)), (1.0, 1.0)),
        "split": build_batch((split,), (1.0,)),
    }
    with BudgetStore(args.budget, Decimal("50")) as budget:

        def charge(name: str, bound: Decimal, call: Callable[[], _T]) -> _T:
            op = output.name + "_" + name
            if budget.ledger.available - bound < Decimal("15"):
                raise ValueError("evaluation reserve would be consumed")
            budget.reserve(op, bound)
            (output / "last_operation.json").write_text(
                json.dumps({"operation": op, "state": "submitted"})
            )
            result = call()
            budget.settle(op, bound)
            (output / "last_operation.json").write_text(
                json.dumps({"operation": op, "state": "confirmed"})
            )
            return result

        service = trio.ServiceClient()
        rest = service.create_rest_client()
        if args.initial_state:
            state_path = str(json.loads(args.initial_state.read_text())["path"])
        else:
            trainer = service.create_lora_training_client(
                base_model="Qwen/Qwen3.5-4B", rank=16, seed=17, train_unembed=False
            )
            (output / "client.json").write_text(
                json.dumps({"training_run_cuid": trainer.training_run_cuid})
            )
            state = charge(
                "initial_state", Decimal(".10"), lambda: trainer.save_state("initial").result()
            )
            (output / "initial_state.json").write_text(state.model_dump_json(indent=2))
            state_path = str(state.path)

        def export(name: str) -> Path:
            saved = charge(
                name + "_export",
                Decimal(".10"),
                lambda: trainer.save_weights_for_sampler(name).result(),
            )
            (output / (name + "_weights.json")).write_text(saved.model_dump_json(indent=2))
            checkpoints = rest.list_checkpoints(trainer.training_run_cuid).result()
            cid = next(c.checkpoint_id for c in checkpoints.checkpoints if c.path == saved.path)
            path = output / (name + ".archive")
            rest.download_checkpoint(cid, path, resume=False).result()
            return path

        baseline = args.baseline if args.baseline else export("baseline")
        deltas: dict[str, float] = {}
        for name, batch in batches.items():
            if args.continue_probe and (output / (name + ".archive")).exists():
                deltas[name] = weight_delta(baseline, output / (name + ".archive"))
                (output / "deltas.json").write_text(json.dumps(deltas, indent=2))
                continue
            trainer = charge(
                name + "_restore",
                Decimal(".01"),
                lambda: service.create_training_client_from_state_with_optimizer(state_path),
            )
            response = charge(
                name + "_fb",
                cost_upper_bound(0, 0, batch.padded_tokens),
                lambda: trainer.forward_backward(  # noqa: B023 - charge is synchronous
                    list(batch.datums),  # noqa: B023 - charge calls synchronously
                    loss_fn="ppo",
                    loss_fn_config=CLIP,
                    auto_shift=False,
                ).result(),
            )
            (output / (name + "_fb.json")).write_text(response.model_dump_json(indent=2))
            # Large epsilon makes this update approximately linear in the gradient.
            charge(
                name + "_optim",
                Decimal(".01"),
                lambda: trainer.optim_step(  # noqa: B023 - charge is synchronous
                    trio.AdamParams(
                        learning_rate=0.001,
                        beta1=0.0,
                        beta2=0.0,
                        eps=1000.0,
                        grad_clip_norm=1e9,
                        weight_decay=0.0,
                    )
                ).result(),
            )
            deltas[name] = weight_delta(baseline, export(name))
            (output / "deltas.json").write_text(json.dumps(deltas, indent=2))
        if deltas["zero"] != 0 or deltas["single"] <= 0:
            raise ValueError("zero mask changed weights or signal failed to update")
        duplicate = deltas["duplicate"] / deltas["single"]
        split_ratio = deltas["split"] / deltas["single"]
        if abs(duplicate - 1) < RATIO_TOLERANCE and abs(split_ratio - 1) < RATIO_TOLERANCE:
            reduction = "assistant_token_mean"
        elif abs(duplicate - 1) < RATIO_TOLERANCE and abs(split_ratio - 0.5) < RATIO_TOLERANCE:
            reduction = "sample_mean"
        elif abs(duplicate - 2) < RATIO_TOLERANCE and abs(split_ratio - 1) < RATIO_TOLERANCE:
            reduction = "sum"
        else:
            raise ValueError(f"unknown reduction: {duplicate}, {split_ratio}")
        result = {
            "mask_passed": True,
            "reduction": reduction,
            "duplicate_ratio": duplicate,
            "split_ratio": split_ratio,
            "deltas": deltas,
            "resume_pending": True,
        }
        (output / "result.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
