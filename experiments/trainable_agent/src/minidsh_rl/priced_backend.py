"""Sampling with calibrated shadow-cost margins; model rendering stays unchanged."""

import asyncio
from typing import cast
from uuid import uuid4

import pytrio as trio  # type: ignore[import-untyped]

from minidsh_rl.backend_v2 import ExpansionBackend, _SDKSampler
from minidsh_rl.contracts import Turn
from minidsh_rl.model import Sample
from minidsh_rl.reprice import milli_upper


class PricedBackend(ExpansionBackend):
    """Same native renderer and sampling parameters, finer financial rounding only."""

    async def sample(self, prompt: tuple[int, ...], limit: int, seed: int) -> Sample:
        """Reserve worst-case spend before RPC; leave unknown outcomes reserved."""
        operation = "sample_" + uuid4().hex
        maximum = milli_upper(len(prompt), limit, 0)
        if self.budget.available - maximum < self.evaluation_reserve:
            raise ValueError("reserved evaluation budget would be consumed")
        await asyncio.to_thread(self.budget.reserve, operation, maximum)
        # SDK lacks py.typed; keep its dynamic methods inside this one adapter boundary.
        sampler = cast("_SDKSampler", self.sampler)
        response = await sampler.sample_async(
            prompt=trio.ModelInput.from_ints(list(prompt)),
            num_samples=1,
            sampling_params=trio.SamplingParams(
                max_tokens=limit, temperature=1.0, top_p=1.0, top_k=-1, seed=seed
            ),
        )
        if len(response.sequences) != 1:
            raise ValueError("sampler returned unexpected number of sequences")
        sequence = response.sequences[0]
        turn = Turn(prompt, tuple(sequence.tokens), tuple(sequence.logprobs))
        if response.input_tokens != len(prompt) or response.output_tokens != len(turn.completion):
            raise ValueError("reported usage does not match actual tokens")
        # Settle at our conservative no-cache upper rate; bill reconciliation may be lower.
        await asyncio.to_thread(
            self.budget.settle, operation, milli_upper(len(prompt), len(turn.completion), 0)
        )
        if sequence.text is None:
            raise ValueError("sampler omitted decoded text")
        return Sample(turn, sequence.text, sequence.stop_reason)
