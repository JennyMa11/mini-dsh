"""Frozen new-instance splits and version-isolated candidate policy."""

from collections import Counter

from minidsh_rl.curve_cases import curve_cases
from minidsh_rl.curve_cases_v2 import curve_cases_v2
from minidsh_rl.learning_tasks import learning_probes
from minidsh_rl.minirepo_verification import candidate_allowed, candidate_rejection_reason


def test_curve_splits_and_fault_combinations() -> None:
    cases = curve_cases()
    assert len(cases) == 30
    assert Counter(c.split for c in cases) == {"train": 12, "dev": 6, "validation": 12}
    assert len({c.task.case_id for c in cases}) == 30
    assert len(learning_probes(cases)) == 210
    for family in {c.task.family for c in cases}:
        group = [c for c in cases if c.task.family == family]
        assert len({c.faults for c in group}) == 10
        assert (
            len({tuple((n, s) for n, s in c.task.files if n in c.task.writable) for c in group})
            == 10
        )
        assert all(len({m.module for m in c.faults}) == 2 for c in group)
    assert curve_cases() == cases


def test_new_policy_does_not_change_legacy() -> None:
    case = next(c for c in curve_cases() if c.task.family == "dependency_batches")
    candidate = dict(case.task.reference)
    candidate["ready.py"] = candidate["ready.py"].replace(
        "graph[node] <= done", "graph[node].issubset(done)"
    )
    assert not candidate_allowed(case.task, candidate)
    assert candidate_allowed(case.task, candidate, policy="curve_pure_v1")
    candidate["ready.py"] += "\ndef leak():\n    return open('secret').read()\n"
    assert not candidate_allowed(case.task, candidate, policy="curve_pure_v1")


def test_v2_adds_only_declared_string_methods() -> None:
    case = next(c for c in curve_cases() if c.task.family == "path_router")
    candidate = dict(case.task.reference)
    candidate["paths.py"] = candidate["paths.py"].replace(".strip('/')", ".rstrip('/')")
    assert not candidate_allowed(case.task, candidate, policy="curve_pure_v1")
    assert candidate_allowed(case.task, candidate, policy="curve_pure_v2")
    candidate["paths.py"] = candidate["paths.py"].replace(".rstrip('/')", ".lstrip('/')")
    assert candidate_allowed(case.task, candidate, policy="curve_pure_v2")
    candidate["paths.py"] = candidate["paths.py"].replace(".lstrip('/')", ".replace('/', '')")
    assert candidate_allowed(case.task, candidate, policy="curve_pure_v2")
    candidate["paths.py"] += "\ndef leak():\n    return open('secret').read()\n"
    assert not candidate_allowed(case.task, candidate, policy="curve_pure_v2")


def test_rejection_reason_identifies_branch_and_location() -> None:
    case = next(c for c in curve_cases() if c.task.family == "path_router")
    candidate = dict(case.task.reference)
    candidate["paths.py"] = candidate["paths.py"].replace(".strip('/')", ".rstrip('/')")
    assert candidate_rejection_reason(case.task, candidate, policy="curve_pure_v1") == {
        "reason": "unsupported_method",
        "module": "paths",
        "line": 2,
        "name": "rstrip",
    }
    candidate["paths.py"] = "def canonical(path):\n    return path(\n"
    assert candidate_rejection_reason(case.task, candidate, policy="curve_pure_v2") == {
        "reason": "syntax_error",
        "detail": "SyntaxError",
    }


def test_v2_changes_only_private_checkers_and_explicit_identity() -> None:
    old, new = curve_cases(), curve_cases_v2()
    for first, second in zip(old, new, strict=True):
        assert second.task.case_id == first.task.case_id.replace("curve_v1_", "curve_v2_")
        assert second.task.files == first.task.files
        assert second.task.reference == first.task.reference
        assert second.faults == first.faults
        assert second.split == first.split
        assert second.dataset_version == "curve_dataset_v2"
        assert second.criterion_version == "curve_private_v2"
        if second.task.family == "path_router":
            assert second.task.hidden_checks != first.task.hidden_checks
            assert "/docs" not in second.task.hidden_checks  # independent private inputs
        else:
            assert second.task.hidden_checks == first.task.hidden_checks
