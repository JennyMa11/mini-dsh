"""Evaluation denominator, task pairing and source-equivalent policy order."""

from minidsh_rl.evaluate_run import evaluation_queue


def test_full_queue_has_validation_and_paired_test_runs_without_best_of_selection() -> None:
    rows = evaluation_queue()
    assert len(rows) == 80
    assert sum(r["split"] == "validation" for r in rows) == 16
    assert sum(r["split"] == "test" for r in rows) == 64
    for i in range(0, len(rows), 2):
        first, second = rows[i : i + 2]
        assert first["case_id"] == second["case_id"]
        assert first["seed"] == second["seed"]
        assert first["repeat"] == second["repeat"]
        assert {first["policy"], second["policy"]} == {"base", "rl"}
    assert rows[0]["policy"] == "base"
    assert rows[2]["policy"] == "rl"
