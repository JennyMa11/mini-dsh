"""New held-out families and private semantic checks must be real before sampling."""

from collections import Counter
from copy import deepcopy

import pytest
from minidsh_rl.gates_v2 import mutating_candidate
from minidsh_rl.task_rules import candidate_allowed
from minidsh_rl.tasks import tasks
from minidsh_rl.tasks_v2 import expansion_tasks, public_files, verifier_source


def test_manifest_and_held_out_families_are_fixed() -> None:
    cases = expansion_tasks()
    assert len(cases) == 96
    assert len({t.case_id for t in cases}) == 96
    assert len({t.reference for t in cases}) == 24
    assert {t.reference for t in cases}.isdisjoint(t.reference for t in tasks())
    assert Counter(t.split for t in cases) == {"train": 48, "validation": 16, "test": 32}
    assert {t.family for t in cases}.isdisjoint(t.family for t in tasks())
    split_families = {
        split: {t.family for t in cases if t.split == split}
        for split in ("train", "validation", "test")
    }
    assert all(
        not split_families[a] & split_families[b]
        for a, b in [("train", "test"), ("train", "validation"), ("test", "validation")]
    )


def test_all_reference_bug_and_constant_wrong_solutions() -> None:
    for task in expansion_tasks():
        assert candidate_allowed(task.reference), task.case_id
        reference: dict[str, object] = {}
        exec(task.reference, reference)
        assert task.check(reference), task.case_id
        verifier = verifier_source(task, "gate_passed")
        for source, expected in [
            (task.reference, True),
            (task.broken, False),
            ("def solve(*args): return " + repr(task.examples[0][1]), False),
        ]:
            namespace: dict[str, object] = {}
            exec(source, namespace)
            # The local gate executes only trusted generated fixture code, never model candidates.
            body = verifier.replace("from solution import solve\n", "")
            passed = True
            try:
                exec(body, namespace)
            except Exception:
                passed = False
            assert passed is expected, (task.case_id, source)


def test_public_fixture_excludes_hidden_checks_and_reference() -> None:
    for task in expansion_tasks():
        before = deepcopy(task.examples)
        fixture = public_files(task)
        assert set(fixture) == {"solution.py", "implementation.py", "smoke_test.py", "README.md"}
        assert task.reference not in fixture.values()
        assert "verify.py" not in fixture
        assert before == task.examples


def test_nonmutation_is_independent_of_return_value() -> None:
    task = next(t for t in expansion_tasks() if t.family == "stable_partition")
    namespace: dict[str, object] = {}
    exec(mutating_candidate(task.reference), namespace)
    with pytest.raises(AssertionError, match=r"^$"):
        exec(verifier_source(task, "marker").replace("from solution import solve\n", ""), namespace)


def test_exception_only_fault_has_visible_reproducer() -> None:
    task = next(t for t in expansion_tasks() if t.family == "clock_minutes" and t.variant == 1)
    fixture = public_files(task)
    body = fixture["smoke_test.py"].replace("from solution import solve\n", "")
    for source, passed in ((task.reference, True), (task.broken, False)):
        namespace: dict[str, object] = {}
        exec(source, namespace)
        actual = True
        try:
            exec(body, namespace)
        except AssertionError:
            actual = False
        assert actual is passed
