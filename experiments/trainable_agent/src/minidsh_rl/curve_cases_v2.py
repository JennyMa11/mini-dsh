"""Private slash-contract correction; frozen v1 inputs/results remain unchanged."""

from dataclasses import replace

from minidsh_rl.curve_cases import curve_cases
from minidsh_rl.learning_contracts import LearningCase

_SLASH_CHECKS = """
# A single route prevents a more specific route masking failed normalization.
for prefix in ('/manual', '/manual/'):
    for path in ('/manual', '/manual/', '/manual/section', '/manual/section/'):
        p = {'routes': [['GET', prefix, 'selected']], 'method': 'get', 'path': path}
        before = {'routes': [['GET', prefix, 'selected']], 'method': 'get', 'path': path}
        assert solve(p) == 'selected', 'private contract: slash equivalence'
        assert p == before, 'private contract: input ownership'
    boundary = {'routes': [['GET', prefix, 'wrong']], 'method': 'GET', 'path': '/manualx'}
    assert solve(boundary) is None
    other = {'routes': [['GET', prefix, 'wrong']], 'method': 'POST', 'path': '/manual/'}
    assert solve(other) is None
assert solve({'routes': [['*', '/', 'root']], 'method': 'POST', 'path': '/elsewhere/'}) == 'root'
"""


def curve_cases_v2() -> tuple[LearningCase, ...]:
    """Same split/defects/public inputs, new case and private-checker version IDs."""
    return tuple(
        replace(
            case,
            task=replace(
                case.task,
                case_id=case.task.case_id.replace("curve_v1_", "curve_v2_"),
                hidden_checks=case.task.hidden_checks
                + (_SLASH_CHECKS if case.task.family == "path_router" else ""),
            ),
            dataset_version="curve_dataset_v2",
            criterion_version="curve_private_v2",
        )
        for case in curve_cases()
    )
