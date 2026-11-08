"""Sixteen bounded text requests for accounting calibration, never a Coding score."""

import argparse
import asyncio
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytrio as trio  # type: ignore[import-untyped]

from minidsh_rl.budget import cost_upper_bound
from minidsh_rl.contracts import Turn
from minidsh_rl.cost_model import PriceCard
from minidsh_rl.cost_store import CostStore
from minidsh_rl.pytrio_backend import Tokenizer, _SDKSampler
from minidsh_rl.recording import FileRecordingSink

REQUESTS = 16
MAX_OUTPUT = 32
CONTEXT_LIMIT = 4096
ROOT = Path("benchmark-results/trainable_agent")


def price_card(quote: Path) -> PriceCard:
    """Validate exact published billing units and model before constructing a client."""
    payload = json.loads(quote.read_text())["prices"]
    if payload["currency"] != "CNY" or payload["token_unit"] != "per_1m_tokens":
        raise ValueError("unsupported billing currency or token unit")
    row = next(r for r in payload["items"] if r["display_name"] == "Qwen/Qwen3.5-4B")
    prices = row["prices"]
    for category, expected in (("prefill", 149), ("sample", 454), ("train", 454)):
        if (
            prices[category]["billing_unit"] != "per_1m_tokens"
            or prices[category]["unit_price"] != expected
        ):
            raise ValueError("quote changed; re-estimate before submission")
    return PriceCard(Decimal("1.49"), Decimal("4.54"), Decimal("4.54"))


def prepare(output: Path, quote: Path) -> PriceCard:
    """Freeze the protocol locally before creating any provider client."""
    card = price_card(quote)
    output.mkdir(parents=True, exist_ok=False)
    (output / "protocol.json").write_text(
        json.dumps(
            {
                "requests": REQUESTS,
                "max_output": MAX_OUTPUT,
                "model": "Qwen/Qwen3.5-4B",
                "seed_base": 91000,
                "coding_rollouts": 0,
                "purpose": "billing calibration only",
                "settlement_mode": "per_rpc_cent_upper_until_billing_rule_confirmed",
                "quote_hash": hashlib.sha256(quote.read_bytes()).hexdigest(),
                "source_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "control_budget_cny": "50",
                "phase_a_cap_cny": "8",
            },
            indent=2,
        )
    )
    return card


async def calibrate(output: Path, quote: Path) -> None:
    """Persist each reserve first; no automatic retry and no unknown usage release."""
    card = await asyncio.to_thread(prepare, output, quote)
    with CostStore(
        ROOT / "expansion-v2/budget.json",
        legacy=ROOT / "core-budget.json",
        quote=quote,
        limit=Decimal("50"),
        uncertainty=Decimal("2"),
    ) as budget:
        # The whole tiny queue must fit without consuming a subsequent coding block.
        reserved_evaluation = Decimal("8")
        if budget.available < REQUESTS * Decimal(".02") + reserved_evaluation:
            raise ValueError("complete calibration and next block do not fit budget")
        service = trio.ServiceClient()
        sampler = service.create_sampling_client(base_model="Qwen/Qwen3.5-4B")
        tokenizer = cast(Tokenizer, sampler.get_tokenizer())
        sdk = cast(_SDKSampler, sampler)
        prompt = tuple(
            tokenizer.apply_chat_template(
                [{"role": "user", "content": "Return only the word OK."}],
                tools=[],
                tokenize=True,
                add_generation_prompt=True,
                enable_thinking=False,
                return_dict=False,
            )
        )
        if not prompt or len(prompt) > CONTEXT_LIMIT:
            raise ValueError("invalid calibration prompt")
        sink = FileRecordingSink(output / "tokens")
        inputs = outputs = 0
        estimate = Decimal("0")
        for index in range(REQUESTS):
            operation = f"{output.name}_sample_{index:03}"
            maximum = cost_upper_bound(len(prompt), MAX_OUTPUT, 0)
            await asyncio.to_thread(budget.reserve, operation, maximum)
            response = await sdk.sample_async(
                prompt=trio.ModelInput.from_ints(list(prompt)),
                num_samples=1,
                sampling_params=trio.SamplingParams(
                    max_tokens=MAX_OUTPUT, temperature=1.0, top_p=1.0, top_k=-1, seed=91000 + index
                ),
            )
            if len(response.sequences) != 1:
                raise ValueError("unexpected response count; pending retained")
            sequence = response.sequences[0]
            if (
                response.input_tokens != len(prompt)
                or response.output_tokens != len(sequence.tokens)
                or not sequence.tokens
                or len(sequence.tokens) > MAX_OUTPUT
                or len(sequence.tokens) != len(sequence.logprobs)
            ):
                raise ValueError("usage mismatch; pending retained")
            Turn(prompt, tuple(sequence.tokens), tuple(sequence.logprobs))
            actual = card.estimate(inputs=len(prompt), outputs=len(sequence.tokens))
            # Preserve the conservative charge until actual billing semantics are attested.
            await asyncio.to_thread(
                budget.settle, operation, cost_upper_bound(len(prompt), len(sequence.tokens), 0)
            )
            await sink.append(
                {
                    "schema_version": 1,
                    "run_id": output.name,
                    "call_index": index,
                    "operation": operation,
                    "prompt_token_ids": list(prompt),
                    "completion_token_ids": sequence.tokens,
                    "old_logprobs": sequence.logprobs,
                    "raw_text": sequence.text,
                    "stop_reason": sequence.stop_reason,
                    "continuous_estimate_cny": str(actual),
                }
            )
            inputs += len(prompt)
            outputs += len(sequence.tokens)
            estimate += actual
        await asyncio.to_thread(
            (output / "summary.json").write_text,
            json.dumps(
                {
                    "completed_requests": REQUESTS,
                    "coding_rollouts": 0,
                    "input_tokens": inputs,
                    "output_tokens": outputs,
                    "continuous_estimate_cny": str(estimate),
                    "global_committed_upper_cny": str(budget.committed),
                    "billing_rule_confirmed": False,
                    "scope": "actual SDK requests; not a coding or training result",
                },
                indent=2,
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quote", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(calibrate(args.output, args.quote))


if __name__ == "__main__":
    main()
