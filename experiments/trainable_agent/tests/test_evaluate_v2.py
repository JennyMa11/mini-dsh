"""The paired protocol and unopened test boundary are fixed before evaluation."""

import json
from pathlib import Path

import pytest
from minidsh_rl.evaluate_v2 import evaluation_queue, policy_paths, require_final_test


def test_validation_pairs_are_complete_matched_and_disjoint_from_test() -> None:
    rows = evaluation_queue("validation")
    test = evaluation_queue("test")
    assert len(rows) == 64
    assert len(test) == 128
    assert not {r["family"] for r in rows} & {r["family"] for r in test}
    for i, (left, right) in enumerate(zip(rows[::2], rows[1::2], strict=True)):
        assert [left["policy"], right["policy"]] == (
            ["base", "rl"] if i % 2 == 0 else ["rl", "base"]
        )
        for key in ("case_id", "repeat", "seed"):
            assert left[key] == right[key]
    with pytest.raises(ValueError, match="validation or test"):
        evaluation_queue("train")


def test_matched_base_is_this_training_initial_adapter(tmp_path: Path) -> None:
    (tmp_path / "summary.json").write_text(json.dumps({"initial_model_path": "trio://initial"}))
    (tmp_path / "final_weights.json").write_text(json.dumps({"path": "trio://final"}))
    assert policy_paths(tmp_path) == {"base": "trio://initial", "rl": "trio://final"}


def test_final_test_cannot_open_without_checkpoint_bound_decision(tmp_path: Path) -> None:
    (tmp_path / "summary.json").write_text("{}")
    with pytest.raises(ValueError, match="recorded decision"):
        require_final_test(tmp_path, None)
    decision = tmp_path / "decision.json"
    decision.write_text(json.dumps({"allow_final_test": True, "parent_summary_sha256": "wrong"}))
    with pytest.raises(ValueError, match="not admitted"):
        require_final_test(tmp_path, decision)
