"""Corrupted policy/seed metadata must not become silently accepted training evidence."""

import json
from pathlib import Path

import pytest
from minidsh_rl.trajectory_audit_v2 import sampled_turns


@pytest.mark.parametrize(
    ("field", "value"), [("policy_id", "other"), ("seed", 18), ("call_index", 1)]
)
def test_changed_policy_seed_or_index_is_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    directory = tmp_path / "tokens"
    directory.mkdir()
    record = {
        "run_id": "run",
        "policy_id": "fixed",
        "seed": 17,
        "call_index": 0,
        "temperature": 1.0,
        "top_p": 1.0,
        "prompt_token_ids": [1, 2],
        "completion_token_ids": [3],
        "old_logprobs": [-0.1],
    }
    record[field] = value
    (directory / "turn_000.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="policy/seed/settings"):
        sampled_turns(tmp_path, run_id="run", policy="fixed", seed=17)


def test_complete_metadata_retains_original_ids_and_logprobs(tmp_path: Path) -> None:
    directory = tmp_path / "tokens"
    directory.mkdir()
    record = {
        "run_id": "run",
        "policy_id": "fixed",
        "seed": 17,
        "call_index": 0,
        "temperature": 1.0,
        "top_p": 1.0,
        "prompt_token_ids": [1, 2],
        "completion_token_ids": [3],
        "old_logprobs": [-0.1],
    }
    (directory / "turn_000.json").write_text(json.dumps(record))
    raw = sampled_turns(tmp_path, run_id="run", policy="fixed", seed=17)
    assert raw[0].prompt == (1, 2)
    assert raw[0].completion == (3,)
    assert raw[0].logprobs == (-0.1,)
