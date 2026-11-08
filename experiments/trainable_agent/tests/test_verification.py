"""Independent verifier payload must reject a candidate and test tampering."""

from minidsh_rl.tasks import tasks
from minidsh_rl.verification import verifier_files


def test_verifier_uses_fixed_expected_values_and_reference_not_exported() -> None:
    task = tasks()[0]
    files = verifier_files(task, task.broken)
    assert files["implementation.py"] == task.broken
    assert "assert" in files["verify.py"]
    assert task.reference not in files["verify.py"]
    assert set(files) == {"solution.py", "implementation.py", "verify.py"}
