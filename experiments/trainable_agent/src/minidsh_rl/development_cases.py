"""Six already-open development cases: independent, actionable public feedback only."""

from dataclasses import replace

from minidsh_rl.curve_cases_v2 import curve_cases_v2
from minidsh_rl.learning_contracts import LearningCase

PublicExample = tuple[str, dict[str, object], object, str | None]


def public_examples(family: str) -> tuple[PublicExample, ...]:
    """Public-spec inputs only; never reads a reference or private checker."""
    if family == "path_router":
        return (
            (
                "root",
                {"routes": [["*", "/", "root"]], "method": "POST", "path": "/child"},
                "root",
                None,
            ),
            (
                "segment_boundary",
                {"routes": [["GET", "/a", "wrong"]], "method": "GET", "path": "/ab"},
                None,
                None,
            ),
            (
                "method_case",
                {"routes": [["get", "/case", "same"]], "method": "GET", "path": "/case"},
                "same",
                None,
            ),
            (
                "longest_prefix",
                {
                    "routes": [["GET", "/a", "short"], ["GET", "/a/b", "long"]],
                    "method": "GET",
                    "path": "/a/b/c",
                },
                "long",
                None,
            ),
            (
                "exact_method_tie",
                {
                    "routes": [["*", "/t", "wild"], ["POST", "/t", "exact"]],
                    "method": "POST",
                    "path": "/t/item",
                },
                "exact",
                None,
            ),
            (
                "declaration_order",
                {
                    "routes": [["GET", "/z", "first"], ["GET", "/z", "last"]],
                    "method": "GET",
                    "path": "/z",
                },
                "first",
                None,
            ),
            (
                "slash_normalization",
                {"routes": [["GET", "/trail/", "ok"]], "method": "GET", "path": "/trail/child/"},
                "ok",
                None,
            ),
            (
                "method_mismatch",
                {"routes": [["GET", "/x", "wrong"]], "method": "POST", "path": "/x"},
                None,
                None,
            ),
        )
    if family == "dependency_batches":
        return (
            ("parallel_batch_sort", {"nodes": ["b", "a"], "edges": []}, [["a", "b"]], None),
            ("edge_direction", {"nodes": ["a", "b"], "edges": [["a", "b"]]}, [["a"], ["b"]], None),
            (
                "duplicate_edges",
                {"nodes": ["b", "a", "c"], "edges": [["a", "c"], ["a", "c"]]},
                [["a", "b"], ["c"]],
                None,
            ),
            ("empty", {"nodes": [], "edges": []}, [], None),
            (
                "cycle_error",
                {"nodes": ["u", "v"], "edges": [["u", "v"], ["v", "u"]]},
                None,
                "ValueError",
            ),
        )
    if family == "table_join":
        return (
            (
                "duplicate_right_matches",
                {"left": [["a", 2]], "right": [["a", 4], ["a", 5]], "mode": "inner"},
                [["a", 2, 4], ["a", 2, 5]],
                None,
            ),
            (
                "left_order",
                {"left": [["x", 1], ["y", 2]], "right": [["y", 8], ["x", 9]], "mode": "inner"},
                [["x", 1, 9], ["y", 2, 8]],
                None,
            ),
            (
                "unmatched_left_value",
                {"left": [["z", 7]], "right": [], "mode": "left"},
                [["z", 7, None]],
                None,
            ),
            ("inner_drops_unmatched", {"left": [["q", 3]], "right": [], "mode": "inner"}, [], None),
            ("empty", {"left": [], "right": [], "mode": "left"}, [], None),
            ("invalid_mode", {"left": [], "right": [], "mode": "outer"}, None, "ValueError"),
        )
    raise ValueError("unsupported development family")


def public_checker(family: str) -> str:
    """Run every independent contract; errors remain failures with bounded diagnostics."""
    return (
        "from service import solve\nfrom copy import deepcopy\n"
        + "examples = "
        + repr(public_examples(family))
        + "\n"
        + """
failures = []
for label, payload, expected, expected_error in examples:
    before = deepcopy(payload)
    try:
        actual = solve(payload)
    except Exception as error:
        if type(error).__name__ != expected_error:
            wanted = expected_error or repr(expected)
            observed = type(error).__name__ + ': ' + str(error)[:100]
            failures.append(f'{label}: expected={wanted} actual={observed}')
    else:
        if expected_error is not None or actual != expected:
            wanted = expected_error or repr(expected)
            failures.append(f'{label}: expected={wanted} actual={repr(actual)[:240]}')
    if payload != before:
        failures.append(f'{label}: expected=unchanged input actual=mutated input')
for message in failures:
    print('contract: ' + message)
if failures:
    raise AssertionError(f'{len(failures)} public checks failed')
print('public tests passed')
"""
    )


def development_cases() -> tuple[LearningCase, ...]:
    """No training/validation cases: these inputs have already been opened for development."""
    return tuple(
        replace(
            case,
            task=replace(
                case.task,
                case_id=case.task.case_id.replace("curve_v2_", "curve_dev_v3_"),
                files=tuple(
                    sorted(
                        (
                            dict(case.task.files)
                            | {"public_tests.py": public_checker(case.task.family)}
                        ).items()
                    )
                ),
            ),
            dataset_version="curve_development_v3",
            public_feedback_version="curve_public_feedback_v3",
        )
        for case in curve_cases_v2()
        if case.split == "dev"
    )
