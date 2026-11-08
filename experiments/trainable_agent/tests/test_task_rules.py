"""Private scoring gates, with explicit positive and malicious negative controls."""

import pytest
from minidsh_rl.task_rules import candidate_allowed, extra_examples, verifier_code
from minidsh_rl.tasks import smoke_tasks, tasks


def test_all_references_satisfy_pure_policy_and_generated_oracles() -> None:
    for task in tasks() + smoke_tasks():
        assert candidate_allowed(task.reference), task.case_id
        namespace: dict[str, object] = {}
        exec(task.reference, namespace)
        check = task.__class__(
            task.family,
            task.variant,
            task.split,
            task.specification,
            task.reference,
            task.broken,
            task.examples + extra_examples(task),
        )
        assert check.check(namespace), task.case_id
        assert "expected ValueError" in verifier_code(task, "marker") or task.family not in {
            "clamp",
            "chunks",
            "moving_sum",
            "pagination",
        }


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef solve(*args): return 0",
        'def solve(*args): return open("verify.py").read()',
        "def solve(*args): return ().__class__.__subclasses__()",
        "def solve(*args): return __builtins__",
    ],
)
def test_oracle_access_and_introspection_rejected(source: str) -> None:
    assert not candidate_allowed(source)
