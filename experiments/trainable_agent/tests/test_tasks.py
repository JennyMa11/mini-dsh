"""Task publication gates: splits, detectable bugs and independent cases."""

from minidsh_rl.tasks import public_fixture, tasks


def test_frozen_split_counts_and_disjoint_families() -> None:
    cases = tasks()
    assert len(cases) == 40
    assert {s: sum(t.split == s for t in cases) for s in ("train", "validation", "test")} == {
        "train": 16,
        "validation": 8,
        "test": 16,
    }
    families: dict[str, set[str]] = {}
    for task in cases:
        families.setdefault(task.family, set()).add(task.split)
    assert all(len(splits) == 1 for splits in families.values())


def test_all_bugs_fail_and_references_pass_frozen_checks() -> None:
    for task in tasks():
        reference: dict[str, object] = {}
        broken: dict[str, object] = {}
        exec(task.reference, reference)
        exec(task.broken, broken)
        assert task.check(reference), task.case_id
        assert not task.check(broken), task.case_id


def test_public_fixture_requires_cross_file_discovery_and_excludes_private_checks() -> None:

    for task in tasks():
        files = public_fixture(task)
        assert files["solution.py"] == "from implementation import solve\n"
        assert files["implementation.py"] == task.broken
        assert set(files) == {"solution.py", "implementation.py", "smoke_test.py", "README.md"}
        assert task.reference not in files.values()
        assert "assert solve" in files["smoke_test.py"]


def test_engineering_fixtures_are_separate_and_detect_their_faults() -> None:
    from minidsh_rl.tasks import smoke_tasks  # noqa: PLC0415 - test the separate export

    cases = smoke_tasks()
    assert len(cases) == 4
    assert {task.family for task in cases}.isdisjoint(task.family for task in tasks())
    for task in cases:
        assert task.split == "smoke"
        reference: dict[str, object] = {}
        broken: dict[str, object] = {}
        exec(task.reference, reference)
        exec(task.broken, broken)
        assert task.check(reference)
        assert not task.check(broken)
        assert set(public_fixture(task)) == {
            "solution.py",
            "implementation.py",
            "smoke_test.py",
            "README.md",
        }
