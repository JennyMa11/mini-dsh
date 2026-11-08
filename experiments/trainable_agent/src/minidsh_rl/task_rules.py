"""Private predeclared checks and a pure-function candidate policy."""

import ast
from itertools import groupby

from minidsh_rl.tasks import Task

_SAFE_CALLS = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "filter",
        "float",
        "int",
        "isinstance",
        "len",
        "list",
        "map",
        "max",
        "min",
        "range",
        "reversed",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
        "ValueError",
        "TypeError",
    }
)
_SAFE_METHODS = frozenset(
    {
        "add",
        "append",
        "clear",
        "copy",
        "count",
        "discard",
        "endswith",
        "extend",
        "get",
        "index",
        "insert",
        "items",
        "join",
        "keys",
        "lower",
        "pop",
        "remove",
        "reverse",
        "setdefault",
        "sort",
        "split",
        "startswith",
        "strip",
        "update",
        "upper",
        "values",
    }
)


def candidate_allowed(source: str) -> bool:  # noqa: PLR0911 - explicit forbidden constructs
    """These tasks need only built-ins; imports, I/O and introspection are forbidden."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return False
    functions = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    if "solve" not in functions:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.AsyncFunctionDef)):
            return False
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            return False
        if isinstance(node, ast.Attribute) and node.attr not in _SAFE_METHODS:
            return False
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in _SAFE_CALLS | functions:
                    return False
            elif not isinstance(node.func, ast.Attribute):
                return False
    return True


def extra_examples(task: Task) -> tuple[tuple[tuple[object, ...], object], ...]:  # noqa: PLR0911
    """Independent oracle values generated before model execution, never from a candidate."""
    family = task.family
    if family == "clamp":
        return tuple(
            ((x, lo, hi), min(hi, max(lo, x)))
            for lo, hi in ((0, 0), (0, 5), (-3, 2))
            for x in (-9, 0, 2, 10)
        )
    if family == "chunks":
        return tuple(
            ((xs, n), [xs[i : i + n] for i in range(0, len(xs), n)])
            for xs in ([], [0], [-2, 0, 3, 4, 5])
            for n in (1, 2, 4, 8)
        )
    if family == "moving_sum":
        return tuple(
            ((xs, n), [sum(w) for w in zip(*(xs[j:] for j in range(n)), strict=False)])
            for xs in ((), (0,), (-2, 1, 4, 0))
            for n in (1, 2, 3, 6)
        )
    if family == "overlay":
        return (
            (({"a": 1}, {"a": None, "b": 0}), {"a": None, "b": 0}),
            (({"z": False}, {}), {"z": False}),
            (({}, {}), {}),
        )
    if family == "runs":
        return tuple(
            ((xs,), [(key, len(list(values))) for key, values in groupby(xs)])
            for xs in ([], [None, None, 0, 0, None], [0, 1, 0, 1], [2, 2, 2, 2])
        )
    if family == "intervals":
        return (
            (([(1, 4), (2, 3), (6, 7)],), [(1, 4), (6, 7)]),
            (([(3, 5), (-2, 0), (0, 3)],), [(-2, 5)]),
            (([(0, 0), (0, 0)],), [(0, 0)]),
        )
    if family == "pagination":
        return tuple(
            ((list(range(7)), page, size), list(range(7))[(page - 1) * size : page * size])
            for page in (1, 2, 4, 8)
            for size in (1, 3, 9)
        )
    if family == "deduplicate":
        return tuple(
            ((xs,), list(dict.fromkeys(xs)))
            for xs in ([], [False, 0, 1, True], [None, None, "a"], [4, 3, 2, 4, 2, 1])
        )
    if family == "transpose":
        matrices: tuple[list[list[int]], ...] = (
            [],
            [[], []],
            [[1, 2]],
            [[1], [2], [3]],
            [[0, -1], [2, 3]],
        )
        return tuple(
            ((matrix,), [list(c) for c in zip(*matrix, strict=False)]) for matrix in matrices
        )
    if family == "group_totals":
        return tuple(
            ((rows,), {key: sum(v for k, v in rows if k == key) for key in dict(rows)})
            for rows in (
                [],
                [("a", 1), ("a", -1)],
                [("b", 0), ("a", -3), ("b", 5)],
                [(None, 2), (None, 3), (0, -4)],
            )
        )
    return ()


def invalid_examples(task: Task) -> tuple[tuple[object, ...], ...]:
    """Only exceptions explicitly required by the task specification are tested."""
    return {
        "clamp": ((0, 2, 1),),
        "chunks": (([], 0), ([], -1)),
        "moving_sum": (([], 0), ([], -1)),
        "pagination": (([], 0, 1), ([], 1, 0), ([], -1, 2)),
    }.get(task.family, ())


def verifier_code(task: Task, marker: str) -> str:
    """Serialize fixed checks, including stated nonmutation and exception contracts."""
    lines = ["from solution import solve", "from copy import deepcopy"]
    immutable = task.family in {"overlay", "intervals"}
    for args, expected in task.examples + extra_examples(task):
        lines += [
            "args=deepcopy(" + repr(args) + ")",
            "before=deepcopy(args)",
            "assert solve(*args) == " + repr(expected),
        ]
        if immutable:
            lines += ["assert args == before"]
    for args in invalid_examples(task):
        lines += [
            "try:",
            "    solve(*" + repr(args) + ")",
            "except ValueError:",
            "    pass",
            "else:",
            '    raise AssertionError("expected ValueError")',
        ]
    lines += ["print(" + repr(marker) + ")"]
    return "\n".join(lines) + "\n"
