"""Repricing must retain non-sampling costs and bind every predecessor hash."""

from decimal import Decimal
from pathlib import Path

import pytest
from minidsh_rl.reprice import milli_upper, reprice_cohort


def test_milli_rounding_retains_margin_without_full_cent_per_category() -> None:
    assert milli_upper(1, 1) == Decimal(".002")
    assert milli_upper(4096, 512) == Decimal(".010")
    assert milli_upper(0, 0, 32768) == Decimal(".149")
    with pytest.raises(ValueError, match="negative"):
        milli_upper(-1, 0)


def test_reprice_empty_or_incomplete_cohort_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cohort"):
        reprice_cohort(tmp_path)


def test_transition_retains_non_sampling_fees_and_original_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib  # noqa: PLC0415 - isolate artifact fixture helpers
    import json  # noqa: PLC0415 - isolate artifact fixture helpers

    from minidsh_rl import reprice  # noqa: PLC0415 - monkeypatch isolated root

    legacy = tmp_path / "core-budget.json"
    legacy.write_text(
        json.dumps({"limit": "50", "spent": "31.38", "pending": {}, "settled": ["prior_admin"]})
    )
    digest = hashlib.sha256(legacy.read_bytes()).hexdigest()
    (tmp_path / "core-analysis-03.json").write_text(
        json.dumps({"provenance": {str(legacy): digest}})
    )
    parents = {}
    for name, n in [
        ("core-train-01", 80),
        ("core-eval-01", 80),
        ("expansion-v2/phase-a/harness-comparison-01", 48),
    ]:
        root = tmp_path / name
        for index in range(n):
            run = root / f"run_{index}"
            (run / "tokens").mkdir(parents=True)
            (run / "result.json").write_text(json.dumps({"run_id": "run", "total_tokens": 3}))
            events = [
                ("run_started", {}),
                ("model_call_completed", {"input_tokens": 2, "output_tokens": 1}),
                ("run_finished", {"status": "finished"}),
            ]
            (run / "trace.jsonl").write_text(
                "\n".join(
                    json.dumps({"name": event, "run_id": "run", "step_id": 1, "data": data})
                    for event, data in events
                )
            )
            (run / "tokens/turn_000.json").write_text(
                json.dumps({"prompt_token_ids": [1, 2], "completion_token_ids": [3]})
            )
            if n == 48:
                parents["sample_" + str(index)] = {"amount": ".02", "status": "estimated"}
    cal = tmp_path / "expansion-v2/phase-a/billing-calibration-01/tokens"
    cal.mkdir(parents=True)
    for index in range(16):
        op = "cal_" + str(index)
        parents[op] = {"amount": ".02", "status": "estimated"}
        (cal / f"{index}.json").write_text(
            json.dumps({"operation": op, "prompt_token_ids": [1, 2], "completion_token_ids": [3]})
        )
    expansion = tmp_path / "expansion-v2/budget.json"
    expansion.write_text(json.dumps({"legacy_hash": digest, "charges": parents, "bills": {}}))
    parent_digest = hashlib.sha256(expansion.read_bytes()).hexdigest()
    quote = tmp_path / "quote.json"
    quote.write_text(
        json.dumps(
            {
                "prices": {
                    "currency": "CNY",
                    "token_unit": "per_1m_tokens",
                    "items": [
                        {
                            "display_name": "Qwen/Qwen3.5-4B",
                            "prices": {
                                k: {"unit_price": v, "billing_unit": "per_1m_tokens"}
                                for k, v in [("prefill", 149), ("sample", 454), ("train", 454)]
                            },
                        }
                    ],
                }
            }
        )
    )
    monkeypatch.setattr(reprice, "ROOT", tmp_path)
    reprice.build(tmp_path / "transition", quote)
    audit = json.loads((tmp_path / "transition/audit.json").read_text())
    assert audit["retained_old_non_sampling_and_s0_cny"] == "28.18"
    assert audit["carried_cny"] == "28.63"
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == digest
    assert hashlib.sha256(expansion.read_bytes()).hexdigest() == parent_digest
