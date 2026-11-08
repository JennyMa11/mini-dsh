"""Tighter financial admission must preserve the exact paired Agent execution."""

import ast
import inspect
import random
from decimal import Decimal

from minidsh_rl import evaluate_final_v2, evaluate_v2
from minidsh_rl.reprice import milli_upper


def test_final_driver_preserves_pair_order_and_execution_function() -> None:
    assert evaluate_final_v2.evaluation_queue("test") == evaluate_v2.evaluation_queue("test")
    assert ast.dump(ast.parse(inspect.getsource(evaluate_final_v2.evaluate))) == ast.dump(
        ast.parse(inspect.getsource(evaluate_v2.evaluate))
    )
    assert Decimal(".06") == evaluate_final_v2.RUN_BOUND


def test_rounded_sampling_cost_fits_token_envelope() -> None:
    rng = random.Random(17)
    for _ in range(10000):
        total_remaining, output_remaining = 24000, 2048
        cost = Decimal(0)
        for _ in range(8):
            output = rng.randint(0, min(output_remaining, 512, total_remaining))
            inputs = rng.randint(0, min(4096 - output, total_remaining - output))
            total_remaining -= inputs + output
            output_remaining -= output
            cost += milli_upper(inputs, output)
        assert cost <= evaluate_final_v2.RUN_BOUND
    continuous = (
        Decimal(24000 - 2048) * Decimal("1.49") + Decimal(2048) * Decimal("4.54")
    ) / 1000000
    assert continuous + Decimal(".016") < evaluate_final_v2.RUN_BOUND
