"""Selection and task filtering cannot leak the held-out test into Base screening."""

from collections import Counter

from minidsh_rl.screen_v2 import screening_queue
from minidsh_rl.tasks_v2 import expansion_tasks


def test_full_screening_queue_excludes_new_test_and_keeps_both_repeats() -> None:
    rows = screening_queue()
    assert len(rows) == 128
    assert Counter(r["split"] for r in rows) == {"train": 96, "validation": 32}
    assert len({r["case_id"] for r in rows}) == 64
    assert {r["case_id"] for r in rows}.isdisjoint(
        t.case_id for t in expansion_tasks() if t.split == "test"
    )
    assert screening_queue() == rows
    for left, right in zip(rows[::2], rows[1::2], strict=True):
        assert left["case_id"] == right["case_id"]
        assert left["seed"] != right["seed"]
        assert {left["repeat"], right["repeat"]} == {0, 1}
