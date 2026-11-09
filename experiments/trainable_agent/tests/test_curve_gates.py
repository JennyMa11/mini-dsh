"""Contract checks before any container/model execution."""

from minidsh_rl.curve_cases import curve_cases
from minidsh_rl.curve_gates import coverage_gate
from minidsh_rl.feedback_coverage import CoverageRow
from minidsh_rl.learning_tasks import learning_probes
from minidsh_rl.minirepo_verification import candidate_allowed


def test_every_probe_is_executable_under_new_policy() -> None:
    cases = curve_cases()
    tasks = {c.task.case_id: c.task for c in cases}
    for probe in learning_probes(cases):
        assert candidate_allowed(
            tasks[probe.case_id], dict(probe.candidate), policy="curve_pure_v1"
        ), (probe.case_id, probe.probe_id)


def test_private_miss_blocks_admission_even_when_public_passes() -> None:
    rows: list[CoverageRow] = [
        {
            "kind": "reference",
            "expected_correct": True,
            "public": {"passed": True, "exit_code": 0, "timed_out": False},
            "private": {"passed": True, "exit_code": 0, "timed_out": False},
        },
        {
            "kind": "semantic",
            "expected_correct": False,
            "public": {"passed": False, "exit_code": 1, "timed_out": False},
            "private": {"passed": True, "exit_code": 0, "timed_out": False},
        },
    ]
    assert not coverage_gate(rows, declared=4)
    rows[1]["private"] = {"passed": False, "exit_code": 1, "timed_out": False}
    assert coverage_gate(rows, declared=4)
    rows[1]["private"]["exit_code"] = -1
    assert not coverage_gate(rows, declared=4)
    assert not coverage_gate(rows[:1], declared=4)
