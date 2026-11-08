"""Mini-repository contracts before implementation: no private oracle in fixtures."""

import ast

from minidsh_rl.minirepo_tasks import development_tasks
from minidsh_rl.minirepo_verification import candidate_allowed


def test_development_task_contract_and_private_split() -> None:
    tasks = development_tasks()
    assert len(tasks) == 8
    assert len({t.family for t in tasks}) == 4
    assert len({t.case_id for t in tasks}) == 8
    for task in tasks:
        files = dict(task.files)
        assert 3 <= len(task.writable) <= 6
        assert 150 <= sum(len(source.splitlines()) for _, source in task.reference) <= 400
        assert set(task.writable) < files.keys()
        assert "public_tests.py" not in task.writable
        assert all(name.endswith(".py") and "/" not in name for name in task.writable)
        assert all("verify_private" not in source for source in files.values())
        assert task.hidden_checks not in "\n".join(files.values())
        assert candidate_allowed(task, dict(task.reference))
        assert candidate_allowed(task, {name: files[name] for name in task.writable})
        assert any(files[name] != dict(task.reference)[name] for name in task.writable)
        assert sum(files[name] != dict(task.reference)[name] for name in task.writable) >= 2
        for source in dict(task.reference).values():
            ast.parse(source)


def test_candidate_policy_allows_project_imports_and_rejects_io_introspection() -> None:
    task = development_tasks()[0]
    candidate = dict(task.reference)
    name = task.writable[0]
    for forbidden in (
        "import os\n",
        "from pathlib import Path\n",
        "from records import __builtins__\n",
        "x = open('secret')\n",
        "x = ().__class__\n",
        "print('verified')\n",
        (
            "def injected():\n    from builtins import open as injected\n"
            "    return injected('verify_private.py')\n"
        ),
    ):
        assert not candidate_allowed(task, candidate | {name: forbidden + candidate[name]})
    assert not candidate_allowed(task, candidate | {"extra.py": "def nope(): return 1\n"})
