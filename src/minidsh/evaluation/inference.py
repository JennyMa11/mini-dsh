"""Derived local-inference measurements from the authoritative trace."""

from __future__ import annotations

from minidsh.evaluation.tracefile import TraceRun


def inference_from_trace(trace: TraceRun | None) -> dict[str, float | None]:
    """Token-weight call speeds and report the highest MLX allocation peak."""
    names = (
        "ttft_ms",
        "prefill_tokens_per_second",
        "decode_tokens_per_second",
        "peak_memory_gb",
    )
    values: dict[str, list[float]] = {name: [] for name in names}
    token_totals = {"prefill_tokens_per_second": 0.0, "decode_tokens_per_second": 0.0}
    second_totals = {"prefill_tokens_per_second": 0.0, "decode_tokens_per_second": 0.0}
    if trace is not None:
        for event in trace.by_name("model_call_completed"):
            metrics = event.data.get("inference")
            if not isinstance(metrics, dict):
                continue
            for name in names:
                value = metrics.get(name)
                if isinstance(value, int | float):
                    values[name].append(float(value))
            for name, count_field in (
                ("prefill_tokens_per_second", "input_tokens"),
                ("decode_tokens_per_second", "output_tokens"),
            ):
                rate = metrics.get(name)
                count = event.data.get(count_field)
                if (
                    isinstance(rate, int | float)
                    and rate > 0
                    and isinstance(count, int)
                    and count > 0
                ):
                    token_totals[name] += count
                    second_totals[name] += count / rate
    result: dict[str, float | None] = {}
    for name, items in values.items():
        if name in token_totals and second_totals[name] > 0:
            result[name] = token_totals[name] / second_totals[name]
        elif not items:
            result[name] = None
        elif name == "peak_memory_gb":
            result[name] = max(items)
        else:
            result[name] = sum(items) / len(items)
    return result
