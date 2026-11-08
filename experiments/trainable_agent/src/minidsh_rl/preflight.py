"""Read-only service/environment evidence. This module never creates a trainer."""

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import cast

from pytrio.config.config import auth_state  # type: ignore[import-untyped]
from pytrio.transport.control.http_client import ControlHTTPClient  # type: ignore[import-untyped]


async def service_evidence(output: Path) -> None:
    """Persist model capabilities and published prices, without credential output."""
    client = ControlHTTPClient(api_key=auth_state.api_key)
    try:
        models = await client.get_models()
        response = await client.http_client.get("model-prices")
        response.raise_for_status()
        prices = cast("dict[str, object]", response.json())
        payload = {
            "schema_version": 1,
            "observed_at": datetime.now(UTC).isoformat(),
            "sdk_version": version("pytrio"),
            "models": [model.model_dump(mode="json") for model in models],
            "prices": prices,
            "scope": "read_only_no_training_or_sampling_submitted",
        }
        await asyncio.to_thread(output.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(output.write_text, json.dumps(payload, indent=2))
    finally:
        await client.aclose()


def estimate_cny(inputs: int, outputs: int, training: int) -> Decimal:
    """2026-08-21 published 4B rates, cents/M converted by official UI /100."""
    if min(inputs, outputs, training) < 0:
        raise ValueError("negative token count")
    return (
        Decimal(inputs) * Decimal("1.49") + Decimal(outputs + training) * Decimal("4.54")
    ) / Decimal(1000000)


def main() -> None:
    """Record read-only capabilities; make no billable model requests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(service_evidence(args.output))
    sys.stdout.write(f"Saved read-only evidence: {args.output}\n")
    sys.stdout.write(hashlib.sha256(args.output.read_bytes()).hexdigest() + "\n")


if __name__ == "__main__":
    main()
