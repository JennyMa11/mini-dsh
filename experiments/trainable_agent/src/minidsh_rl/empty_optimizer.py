"""Verify a never-updated optimizer without unpickling arbitrary Python objects."""

import io
import tarfile
from pathlib import Path
from typing import Any

MAX_OPTIMIZER_BYTES = 1024 * 1024


def empty_optimizer(path: Path) -> list[dict[str, Any]]:
    import torch  # noqa: PLC0415 - optional lab extra only needed by this explicit protocol

    with tarfile.open(path) as archive:
        members = [m for m in archive.getmembers() if m.isfile() and m.name == "optimizer.pt"]
        if len(members) != 1 or members[0].size > MAX_OPTIMIZER_BYTES:
            raise ValueError("missing or oversized initial optimizer")
        stream = archive.extractfile(members[0])
        if stream is None:
            raise ValueError("missing initial optimizer")
        value = torch.load(io.BytesIO(stream.read()), weights_only=True, map_location="cpu")
    if (
        not isinstance(value, dict)
        or set(value) != {"state", "param_groups"}
        or value["state"] != {}
    ):
        raise ValueError("optimizer has nonempty or unknown accumulated state")
    groups = value["param_groups"]
    if not isinstance(groups, list) or not groups:
        raise ValueError("optimizer groups missing")
    for group in groups:
        if (
            not isinstance(group, dict)
            or not isinstance(group.get("params"), list)
            or not group["params"]
            or len(set(group["params"])) != len(group["params"])
        ):
            raise ValueError("initial optimizer parameters invalid")
    return [
        {k: v for k, v in group.items() if k != "params"}
        | {"parameter_count": len(group["params"])}
        for group in groups
    ]


def compare_empty_optimizers(reference: Path, candidate: Path) -> None:
    if empty_optimizer(reference) != empty_optimizer(candidate):
        raise ValueError("initial optimizer groups changed")
