"""Frozen synthetic tasks. Reference and checks stay outside agent workspaces."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True, slots=True)
class Task:
    """One independently verifiable mutation; related mutations share a split."""

    family: str
    variant: int
    split: str
    specification: str
    reference: str
    broken: str
    examples: tuple[tuple[tuple[object, ...], object], ...]

    @property
    def case_id(self) -> str:
        return f"{self.family}_{self.variant}"

    def check(self, namespace: dict[str, object]) -> bool:
        """Evaluate predetermined inputs, never model-reported success."""
        function = cast("Callable[..., object]", namespace["solve"])
        try:
            return all(function(*args) == expected for args, expected in self.examples)
        except Exception:
            return False


# Each family has four independently seeded faults in the same implementation.
# These are pure Python fixture data, not runtime code supplied to production.
Family = tuple[
    str,
    str,
    str,
    str,
    tuple[tuple[str, str], ...],
    tuple[tuple[tuple[object, ...], object], ...],
]

_FAMILIES: tuple[Family, ...] = (
    (
        "clamp",
        "train",
        "Clamp x into inclusive [low, high]; raise ValueError for reversed bounds.",
        (
            "def solve(x, low, high):\n    if low > high:\n        raise Va"
            'lueError("bounds")\n    if x < low:\n        return low\n    if'
            " x > high:\n        return high\n    return x\n"
        ),
        (
            ("return low", "return high"),
            ("return high", "return low"),
            ("return x", "return low"),
            ("x < low", "x > low"),
        ),
        (((-2, 0, 5), 0), ((9, 0, 5), 5), ((3, 0, 5), 3), ((0, 0, 5), 0), ((5, 0, 5), 5)),
    ),
    (
        "chunks",
        "train",
        (
            "Split a list into consecutive chunks of size n, including a "
            "short last chunk; n must be positive."
        ),
        (
            'def solve(xs, n):\n    if n <= 0:\n        raise ValueError("s'
            'ize")\n    out = []\n    for i in range(0, len(xs), n):\n      '
            "  out.append(xs[i:i+n])\n    return out\n"
        ),
        (
            ("range(0,", "range(1,"),
            ("len(xs), n", "len(xs)-1, n"),
            ("i:i+n", "i:i+n-1"),
            ("return out", "return out[::-1]"),
        ),
        ((((1, 2, 3, 4, 5), 2), [(1, 2), (3, 4), (5,)]), (((1,), 2), [(1,)]), (((), 2), [])),
    ),
    (
        "overlay",
        "train",
        (
            "Merge mappings left then right; right wins collisions, inclu"
            "ding None values. Do not mutate inputs."
        ),
        (
            "def solve(left, right):\n    out = dict(left)\n    for key, va"
            "lue in right.items():\n        out[key] = value\n    return ou"
            "t\n"
        ),
        (
            ("dict(left)", "{}"),
            ("right.items()", "left.items()"),
            ("out[key] = value", "out.setdefault(key, value)"),
            ("out[key] = value", "out[key] = value if value is not None else 0"),
        ),
        (
            (({"a": 1}, {"b": 2}), {"a": 1, "b": 2}),
            (({"a": 1}, {"a": 3}), {"a": 3}),
            (({}, {"x": None}), {"x": None}),
        ),
    ),
    (
        "moving_sum",
        "train",
        (
            "For each complete width-n window, return its sum. Empty or s"
            "horter inputs yield []; n must be positive."
        ),
        (
            'def solve(xs, n):\n    if n <= 0:\n        raise ValueError("w'
            'idth")\n    out = []\n    for i in range(len(xs)-n+1):\n       '
            " out.append(sum(xs[i:i+n]))\n    return out\n"
        ),
        (
            ("len(xs)-n+1", "len(xs)-n"),
            ("i:i+n", "i:i+n-1"),
            ("sum(xs[i:i+n])", "sum(xs[i:i+n]) / n"),
            ("return out", "return out[::-1]"),
        ),
        ((((1, 2, 4), 2), [3, 6]), (((4,), 1), [4]), (((), 2), [])),
    ),
    (
        "runs",
        "validation",
        (
            "Run-length encode consecutive equal items as (item,count), k"
            "eeping order; [] becomes []."
        ),
        (
            "def solve(xs):\n    out = []\n    for x in xs:\n        if out "
            "and out[-1][0] == x:\n            out[-1] = (x, out[-1][1]+1)"
            "\n        else:\n            out.append((x, 1))\n    return out"
            "\n"
        ),
        (
            ("== x", "!= x"),
            ("[1]+1", "[1]+2"),
            ("(x, 1)", "(x, 0)"),
            ("return out", "return out[:-1]"),
        ),
        (
            ((("a", "a", "b", "a"),), [("a", 2), ("b", 1), ("a", 1)]),
            (((),), []),
            (((1,),), [(1, 1)]),
        ),
    ),
    (
        "intervals",
        "validation",
        (
            "Sort and merge closed intervals; touching endpoints merge. I"
            "nput order is arbitrary; do not mutate input."
        ),
        (
            "def solve(items):\n    out = []\n    for lo, hi in sorted(item"
            "s):\n        if out and lo <= out[-1][1]:\n            out[-1]"
            " = (out[-1][0], max(hi, out[-1][1]))\n        else:\n         "
            "   out.append((lo, hi))\n    return out\n"
        ),
        (
            ("sorted(items)", "items"),
            ("lo <=", "lo <"),
            ("max(hi,", "min(hi,"),
            ("out[-1][0], max", "lo, max"),
        ),
        (((((5, 8), (1, 3), (3, 6)),), [(1, 8)]), (((((1, 8), (2, 4))),), [(1, 8)]), (((),), [])),
    ),
    (
        "pagination",
        "test",
        (
            "Return page of items using 1-based pages and positive page s"
            "ize; invalid arguments raise ValueError."
        ),
        (
            "def solve(xs, page, size):\n    if page < 1 or size < 1:\n    "
            '    raise ValueError("page")\n    start = (page-1)*size\n    r'
            "eturn xs[start:start+size]\n"
        ),
        (
            ("(page-1)*size", "page*size"),
            ("start:start+size", "start:start+size-1"),
            ("(page-1)*size", "page-1"),
            ("return xs[start:start+size]", "return xs[start:]"),
        ),
        (
            (((0, 1, 2, 3, 4, 5), 2, 2), (2, 3)),
            (((0, 1, 2, 3, 4, 5), 1, 2), (0, 1)),
            (((0,), 3, 2), ()),
        ),
    ),
    (
        "deduplicate",
        "test",
        "Remove duplicates preserving first occurrence order. Values are hashable.",
        (
            "def solve(xs):\n    seen = set()\n    out = []\n    for x in xs"
            ":\n        if x not in seen:\n            seen.add(x)\n        "
            "    out.append(x)\n    return out\n"
        ),
        (
            ("x not in seen", "x in seen"),
            ("seen.add(x)", "seen.clear()"),
            ("out.append(x)", "out.insert(0,x)"),
            ("return out", "return out[1:]"),
        ),
        ((((3, 1, 3, 2, 1),), [3, 1, 2]), (((),), []), (((0, 0),), [0])),
    ),
    (
        "transpose",
        "test",
        ("Transpose a rectangular non-ragged matrix. Empty input yields []; return list of lists."),
        (
            "def solve(matrix):\n    if not matrix:\n        return []\n    "
            "return [[row[j] for row in matrix] for j in range(len(matrix"
            "[0]))]\n"
        ),
        (
            ("len(matrix[0])", "len(matrix)"),
            ("row[j]", "row[0]"),
            ("for row in matrix", "for row in reversed(matrix)"),
            ("range(len(matrix[0]))", "range(1,len(matrix[0]))"),
        ),
        (((((1, 2, 3), (4, 5, 6)),), [[1, 4], [2, 5], [3, 6]]), (((),), []), ((((7,),),), [[7]])),
    ),
    (
        "group_totals",
        "test",
        (
            "Sum integer amounts grouped by key, including zero and negat"
            "ive values. Empty input yields {}."
        ),
        (
            "def solve(rows):\n    totals = {}\n    for key, amount in rows"
            ":\n        totals[key] = totals.get(key, 0) + amount\n    retu"
            "rn totals\n"
        ),
        (
            ("totals.get(key, 0) + amount", "amount"),
            ("+ amount", "- amount"),
            ("get(key, 0)", "get(key, 1)"),
            ("totals[key] =", "totals[str(key)] ="),
        ),
        (((((1, 2), (1, 3), (2, -1)),), {1: 5, 2: -1}), (((),), {}), (((("a", 0),),), {"a": 0})),
    ),
)


def tasks() -> tuple[Task, ...]:
    """Return all 40 predeclared mutations, never selecting by model outcome."""
    result = []
    for family, split, spec, reference, mutations, examples in _FAMILIES:
        for variant, (before, after) in enumerate(mutations):
            if before not in reference:
                raise ValueError("mutation does not match")
            result.append(
                Task(
                    family,
                    variant,
                    split,
                    spec,
                    reference,
                    reference.replace(before, after, 1),
                    examples,
                )
            )
    return tuple(result)


def public_fixture(task: Task) -> dict[str, str]:
    """Export only the broken program and one visible reproducer, never the oracle."""
    namespace: dict[str, object] = {}
    exec(task.broken, namespace)
    function = cast("Callable[..., object]", namespace["solve"])
    visible = None
    for args, expected in task.examples:
        try:
            passed = function(*args) == expected
        except Exception:
            passed = False
        if not passed:
            visible = (args, expected)
            break
    if visible is None:
        raise ValueError("task has no visible failing reproducer")
    args, expected = visible
    return {
        "solution.py": "from implementation import solve\n",
        "implementation.py": task.broken,
        "smoke_test.py": "from solution import solve\n"
        + f"assert solve(*{args!r}) == {expected!r}\n",
        "README.md": (
            "# Repair task\n"
            + task.specification
            + "\nEntry point: solution.solve. Find the implementation dependency. "
            "Only implementation.py may change. Run python3 smoke_test.py. "
            "The final implementation must use only built-ins: no imports, I/O or introspection.\n"
        ),
    }


def smoke_tasks() -> tuple[Task, ...]:
    """Four engineering-only tasks, excluded from training/validation/test metrics."""
    definitions: tuple[
        tuple[str, str, str, str, tuple[tuple[tuple[object, ...], object], ...]], ...
    ] = (
        (
            "smoke_distance",
            "Return the nonnegative absolute difference of a and b.",
            "def solve(a,b):\n    return abs(a-b)\n",
            "def solve(a,b):\n    return a+b\n",
            (((4, 1), 3), ((-2, 3), 5), ((7, 7), 0)),
        ),
        (
            "smoke_last_even",
            "Return the last even integer, or None if no even integer exists.",
            "def solve(xs):\n    found = None\n    for x in xs:\n"
            "        if x % 2 == 0:\n            found = x\n    return found\n",
            "def solve(xs):\n    for x in xs:\n        if x % 2 == 0:\n"
            "            return x\n    return None\n",
            ((((2, 3, 4),), 4), (((1, 3),), None), (((0,),), 0)),
        ),
        (
            "smoke_prefix_sum",
            "Return the sum of the first n numbers, n is nonnegative.",
            "def solve(xs,n):\n    return sum(xs[:n])\n",
            "def solve(xs,n):\n    return sum(xs[n:])\n",
            ((((1, 2, 5), 2), 3), (((3, 4), 0), 0), (((), 2), 0)),
        ),
        (
            "smoke_defaults",
            "Use fallback only when supplied is None; preserve all other values.",
            (
                "def solve(supplied,fallback):\n"
                "    return fallback if supplied is None else supplied\n"
            ),
            "def solve(supplied,fallback):\n    return supplied or fallback\n",
            (((0, 7), 0), ((None, 7), 7), (("", "x"), "")),
        ),
    )
    return tuple(
        Task(family, 0, "smoke", spec, reference, broken, examples)
        for family, spec, reference, broken, examples in definitions
    )
