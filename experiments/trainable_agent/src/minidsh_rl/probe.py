"""One bounded, text-only sampler/logprob probe; never executes model tools."""

import argparse
import asyncio
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytrio as trio  # type: ignore[import-untyped]

from minidsh.domain import Message, ModelRequest, Role
from minidsh_rl.budget import BudgetStore, cost_upper_bound
from minidsh_rl.pytrio_backend import PyTrioBackend, Tokenizer

LOGPROB_TOLERANCE = 0.001


async def probe(output: Path, budget_path: Path) -> None:
    """Run once in a fresh directory; unknown outcomes retain their reservations."""
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    # Keep service and actor clients alive throughout both operations; no auto retry here.
    service = trio.ServiceClient()
    sampler = service.create_sampling_client(base_model="Qwen/Qwen3.5-4B")
    tokenizer = cast("Tokenizer", sampler.get_tokenizer())
    with BudgetStore(budget_path, Decimal("50")) as budget:
        backend = PyTrioBackend(sampler, tokenizer, budget, "base_qwen35_4b")
        identity = backend.identity()
        request = ModelRequest((Message(Role.USER, "Reply with exactly the word OK."),))
        prompt = backend.render(request)
        sample = await backend.sample(prompt, 32, 17)
        await asyncio.to_thread(
            (output / "sample.json").write_text,
            json.dumps(
                {
                    "identity": identity,
                    "turn": asdict(sample.turn),
                    "text": sample.text,
                    "stop_reason": sample.stop_reason,
                },
                indent=2,
            ),
        )
        all_tokens = list(prompt + sample.turn.completion)
        operation = "probe_logprob_" + output.name
        maximum = cost_upper_bound(len(all_tokens), 0, len(all_tokens))
        await asyncio.to_thread(budget.reserve, operation, maximum)
        recomputed = await sampler.compute_logprobs_async(trio.ModelInput.from_ints(all_tokens))
        if len(recomputed) != len(all_tokens):
            raise ValueError("recomputed probability count mismatch")
        await asyncio.to_thread(budget.settle, operation, maximum)
        actual = recomputed[len(prompt) :]
        if any(x is None for x in actual):
            raise ValueError("missing recomputed assistant logprob")
        differences = [
            abs(float(x) - old) for x, old in zip(actual, sample.turn.logprobs, strict=True)
        ]
        result = {
            "sampled_tokens": len(sample.turn.completion),
            "max_logprob_error": max(differences),
            "threshold": LOGPROB_TOLERANCE,
            "passed": max(differences) <= LOGPROB_TOLERANCE,
            "conservative_spend_cny": str(budget.ledger.spent),
            "pending_operations": list(budget.ledger.pending),
        }
        await asyncio.to_thread((output / "result.json").write_text, json.dumps(result, indent=2))
        if not result["passed"]:
            raise ValueError("sample/recompute probability mismatch; investigate before training")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.output, args.budget))


if __name__ == "__main__":
    main()
