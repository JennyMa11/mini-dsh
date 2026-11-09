"""Feedback-only revision preserves every prior bug and sealed private check."""

from minidsh_rl.curve_cases_v2 import curve_cases_v2
from minidsh_rl.development_cases import development_cases, public_examples
from minidsh_rl.learning_tasks import learning_probes


def test_development_only_feedback_revision() -> None:
    old = [c for c in curve_cases_v2() if c.split == "dev"]
    new = development_cases()
    assert len(new) == 6
    assert len(learning_probes(new)) == 42
    for before, after in zip(old, new, strict=True):
        assert after.dataset_version == "curve_development_v3"
        assert after.split == "dev"
        assert after.faults == before.faults
        assert after.task.reference == before.task.reference
        assert after.task.hidden_checks == before.task.hidden_checks
        assert after.criterion_version == before.criterion_version
        assert after.task.specification == before.task.specification
        changed = {name for name, text in after.task.files if dict(before.task.files)[name] != text}
        assert changed == {"public_tests.py"}
        assert "/manual" not in dict(after.task.files)["public_tests.py"]


def test_public_checks_are_separate_and_bounded() -> None:
    for case in development_cases():
        examples = public_examples(case.task.family)
        assert len({e[0] for e in examples}) == len(examples)
        source = dict(case.task.files)["public_tests.py"]
        assert len(source) < 5000
        assert "except Exception" in source
        assert "public checks failed" in source
        assert "expected=" in source
        assert "actual=" in source
