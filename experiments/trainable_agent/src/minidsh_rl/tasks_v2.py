"""Private v2 fixture definitions; literal independent oracles never enter workspaces."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from textwrap import dedent
from typing import cast

from minidsh_rl.tasks import Task, public_fixture

VARIANTS_PER_FAMILY = 4
Args = tuple[object, ...]
Example = tuple[Args, object]


@dataclass(frozen=True, slots=True)
class ExpansionTask(Task):
    """Task-compatible public fixture plus private exceptions/nonmutation contract."""

    invalid_inputs: tuple[Args, ...] = ()
    preserve_inputs: bool = True


@dataclass(frozen=True, slots=True)
class FamilySpec:
    name: str
    split: str
    specification: str
    source: str
    faults: tuple[tuple[str, str], ...]
    examples: tuple[Example, ...]
    invalid: tuple[Args, ...] = ()


_FAMILIES: tuple[FamilySpec, ...] = (
    FamilySpec(
        "bisect_left",
        "train",
        "Return the leftmost insertion index in an ascending list, including duplicate values.",
        """
    def solve(xs, target):
        lo, hi = 0, len(xs)
        while lo < hi:
            mid = (lo + hi) // 2
            if xs[mid] < target:
                lo = mid + 1
            else:
                hi = mid
        return lo
    """,
        (
            ("xs[mid] < target", "xs[mid] <= target"),
            ("return lo", "return max(0, lo-1)"),
            ("len(xs)", "max(0, len(xs)-1)"),
            ("return lo", "return min(len(xs), lo+1)"),
        ),
        (
            (([1, 3, 3, 8], 3), 1),
            (([1, 3, 3, 8], 4), 3),
            (([], 9), 0),
            (([1, 2], 0), 0),
            (([1, 2], 9), 2),
            (([-3, 0, 4], 0), 1),
        ),
    ),
    FamilySpec(
        "horner_polynomial",
        "train",
        "Evaluate coefficients in descending power order at x. Empty coefficients evaluate to 0.",
        """
    def solve(coefficients, x):
        value = 0
        for c in coefficients:
            value = value * x + c
        return value
    """,
        (
            ("value * x + c", "value + x * c"),
            ("value = 0", "value = 1"),
            ("in coefficients:", "in coefficients[::-1]:"),
            ("return value", "return value * x"),
        ),
        (
            (([2, 3, 4], 2), 18),
            (([], 3), 0),
            (([5], 0), 5),
            (([1, 0, -1], -2), 3),
            (([1, 2], 0), 2),
        ),
    ),
    FamilySpec(
        "gcd_integer",
        "train",
        "Return the nonnegative greatest common divisor of two integers, gcd(0,0)=0.",
        """
    def solve(a, b):
        a, b = abs(a), abs(b)
        while b:
            a, b = b, a % b
        return a
    """,
        (
            ("abs(a), abs(b)", "a, b"),
            ("return a", "return b"),
            ("while b:", "while b > 1:"),
            ("return a", "return a + 1"),
        ),
        (((24, 18), 6), ((-12, 8), 4), ((0, 0), 0), ((0, 7), 7), ((13, 5), 1), ((-4, -8), 4)),
    ),
    FamilySpec(
        "bracket_stack",
        "train",
        "Return whether (), [] and {} are balanced and correctly nested; ignore other characters.",
        """
    def solve(text):
        stack = []
        pairs = {')':'(', ']':'[', '}':'{'}
        for c in text:
            if c in '([{':
                stack.append(c)
            elif c in pairs:
                if not stack or stack.pop() != pairs[c]:
                    return False
        return not stack
    """,
        (
            ("return not stack", "return True"),
            ("stack.pop() != pairs[c]", "stack.pop() == pairs[c]"),
            ("c in '([{':", "c in '([':"),
            ("return False", "return True"),
        ),
        (
            (("([])",), True),
            (("([)]",), False),
            (("(",), False),
            (("]",), False),
            (("abc",), True),
            (("{a[b](c)}",), True),
        ),
    ),
    FamilySpec(
        "edit_distance",
        "train",
        (
            "Return Levenshtein distance between two strings with unit insertion, deletion "
            "and substitution costs."
        ),
        """
    def solve(a, b):
        row = list(range(len(b)+1))
        for i, x in enumerate(a, 1):
            nxt = [i]
            for j, y in enumerate(b, 1):
                nxt.append(min(nxt[-1]+1, row[j]+1, row[j-1]+(x != y)))
            row = nxt
        return row[-1]
    """,
        (
            ("(x != y)", "(x == y)"),
            ("nxt = [i]", "nxt = [0]"),
            ("row[j]+1", "row[j]+2"),
            ("return row[-1]", "return row[0]"),
        ),
        (
            (("kitten", "sitting"), 3),
            (("", "abc"), 3),
            (("abc", ""), 3),
            (("abc", "abc"), 0),
            (("a", "b"), 1),
            (("ab", "a"), 1),
        ),
    ),
    FamilySpec(
        "flood_component",
        "train",
        (
            "For a rectangular binary grid return the size of the 4-connected component of 1s "
            "at (r,c); invalid positions or a 0 cell give 0. Do not mutate grid."
        ),
        """
    def solve(grid, r, c):
        if not grid or r < 0 or c < 0 or r >= len(grid) or c >= len(grid[0]) or not grid[r][c]:
            return 0
        seen, pending = set(), [(r,c)]
        while pending:
            x,y = pending.pop()
            if ((x,y) in seen
                or not (0 <= x < len(grid) and 0 <= y < len(grid[0]))
                or not grid[x][y]):
                continue
            seen.add((x,y))
            pending.extend([(x-1,y),(x+1,y),(x,y-1),(x,y+1)])
        return len(seen)
    """,
        (
            ("return len(seen)", "return len(seen)+1"),
            ("(x,y+1)", "(x,y-1)"),
            ("(x+1,y)", "(x-1,y)"),
            ("return len(seen)", "return min(1, len(seen))"),
        ),
        (
            (([[1, 1, 0], [0, 1, 0], [1, 0, 1]], 0, 0), 3),
            (([[0]], 0, 0), 0),
            (([[1]], 0, 0), 1),
            (([], 0, 0), 0),
            (([[1, 1], [1, 1]], 0, 0), 4),
            (([[1]], -1, 0), 0),
        ),
    ),
    FamilySpec(
        "stable_partition",
        "train",
        (
            "Return negative values followed by nonnegative values, preserving order within "
            "both parts, without mutating input."
        ),
        """
    def solve(xs):
        left, right = [], []
        for x in xs:
            if x < 0:
                left.append(x)
            else:
                right.append(x)
        return left + right
    """,
        (
            ("x < 0", "x <= 0"),
            ("return left + right", "return right + left"),
            ("left.append(x)", "left.insert(0,x)"),
            ("right.append(x)", "right.insert(0,x)"),
        ),
        (
            (([2, -1, 0, -3, 4],), [-1, -3, 2, 0, 4]),
            (([],), []),
            (([-2, -1],), [-2, -1]),
            (([0, 1],), [0, 1]),
            (([1, 0, -1],), [-1, 1, 0]),
        ),
    ),
    FamilySpec(
        "matrix_product",
        "train",
        (
            "Multiply compatible nonempty rectangular integer matrices A and B. Inputs are "
            "valid; do not mutate them."
        ),
        """
    def solve(a,b):
        return [[sum(a[i][k]*b[k][j] for k in range(len(b))) for j in range(len(b[0]))]
                for i in range(len(a))]
    """,
        (
            ("a[i][k]*b[k][j]", "a[i][k]+b[k][j]"),
            ("range(len(b))", "range(max(0,len(b)-1))"),
            ("for i in range(len(a))", "for i in range(max(0,len(a)-1))"),
            ("a[i][k]*b[k][j]", "a[0][k]*b[k][j]"),
        ),
        (
            (([[1, 2], [3, 4]], [[5, 6], [7, 8]]), [[19, 22], [43, 50]]),
            (([[2, 3]], [[4], [5]]), [[23]]),
            (([[0]], [[9]]), [[0]]),
            (([[-2]], [[3]]), [[-6]]),
        ),
    ),
    FamilySpec(
        "merge_ordered",
        "train",
        (
            "Merge two ascending lists into an ascending list preserving duplicates, without "
            "mutating inputs."
        ),
        """
    def solve(a,b):
        out, i, j = [], 0, 0
        while i < len(a) and j < len(b):
            if a[i] <= b[j]:
                out.append(a[i]); i += 1
            else:
                out.append(b[j]); j += 1
        return out + a[i:] + b[j:]
    """,
        (
            ("a[i] <= b[j]", "a[i] >= b[j]"),
            ("out + a[i:] + b[j:]", "out + a[i:]"),
            ("out + a[i:] + b[j:]", "out + b[j:]"),
            ("return out + a[i:] + b[j:]", "return (out + a[i:] + b[j:])[::-1]"),
        ),
        (
            (([1, 3], [2, 4]), [1, 2, 3, 4]),
            (([], [1, 2]), [1, 2]),
            (([1, 2], []), [1, 2]),
            (([1, 1], [1]), [1, 1, 1]),
            (([], []), []),
        ),
    ),
    FamilySpec(
        "flatten_nested",
        "train",
        (
            "Flatten arbitrarily nested lists into a list of leaves; only lists are "
            "containers. Preserve leaf types and order; do not mutate input."
        ),
        """
    def solve(xs):
        out = []
        for x in xs:
            if isinstance(x,list):
                out.extend(solve(x))
            else:
                out.append(x)
        return out
    """,
        (
            ("out.extend(solve(x))", "out.append(solve(x))"),
            ("out.append(x)", "out.insert(0,x)"),
            ("return out", "return out[::-1]"),
            ("isinstance(x,list)", "isinstance(x,(list,tuple))"),
        ),
        (
            (([1, [2, [], [3]], 4],), [1, 2, 3, 4]),
            (([],), []),
            (([[[]]],), []),
            (([(1, 2), [None, False]],), [(1, 2), None, False]),
            ((["ab", ["c"]],), ["ab", "c"]),
        ),
    ),
    FamilySpec(
        "decode_runs",
        "train",
        (
            "Expand (value,count) pairs into a list. Counts are nonnegative integers; zero "
            "contributes nothing."
        ),
        """
    def solve(pairs):
        out = []
        for value, count in pairs:
            out.extend([value]*count)
        return out
    """,
        (
            ("[value]*count", "[value]*(count+1)"),
            ("out.extend([value]*count)", "out.append([value]*count)"),
            ("return out", "return out[::-1]"),
            ("[value]*count", "[value]*max(0,count-1)"),
        ),
        (
            (([("a", 2), ("b", 1), ("a", 1)],), ["a", "a", "b", "a"]),
            (([],), []),
            (([(None, 0), (0, 2)],), [0, 0]),
            (([(False, 1)],), [False]),
        ),
    ),
    FamilySpec(
        "top_values",
        "train",
        (
            "Return the k largest values in descending order with duplicates. k=0 gives []; "
            "k<0 raises ValueError. Do not mutate input."
        ),
        """
    def solve(xs,k):
        if k < 0:
            raise ValueError('k')
        return sorted(xs, reverse=True)[:k]
    """,
        (
            ("reverse=True", "reverse=False"),
            ("[:k]", "[k:]"),
            ("k < 0", "k > 0"),
            ("[:k]", "[:max(0,k-1)]"),
        ),
        (
            (([1, 5, 5, 2], 2), [5, 5]),
            (([1, 2], 0), []),
            (([], 4), []),
            (([-3, -1, -2], 9), [-1, -2, -3]),
            (([7], 1), [7]),
        ),
        (([], -1),),
    ),
    FamilySpec(
        "alphabet_shift",
        "validation",
        "Shift lowercase ASCII letters by k modulo 26. Preserve all other characters.",
        """
    def solve(text,k):
        alphabet = 'abcdefghijklmnopqrstuvwxyz'
        out = []
        for c in text:
            if c in alphabet:
                out.append(alphabet[(alphabet.index(c)+k)%26])
            else:
                out.append(c)
        return ''.join(out)
    """,
        (
            ("+k", "-k"),
            ("%26", "%25"),
            ("out.append(c)", "out.append('')"),
            ("return ''.join(out)", "return ''.join(out[::-1])"),
        ),
        ((("xyz!", 3), "abc!"), (("aZ-0", -1), "zZ-0"), (("", 7), ""), (("abc", 26), "abc")),
    ),
    FamilySpec(
        "common_prefix",
        "validation",
        (
            "Return the longest common string prefix of a list of strings; empty list gives "
            "empty string."
        ),
        """
    def solve(xs):
        if not xs:
            return ''
        prefix = xs[0]
        for text in xs[1:]:
            while not text.startswith(prefix):
                prefix = prefix[:-1]
        return prefix
    """,
        (
            ("xs[1:]", "xs[2:]"),
            ("return prefix", "return prefix[:-1]"),
            ("prefix = xs[0]", "prefix = xs[-1]"),
            ("return prefix", "return prefix + 'x'"),
        ),
        (
            ((["flower", "flow", "flight"],), "fl"),
            (([],), ""),
            ((["abc"],), "abc"),
            ((["dog", "cat"],), ""),
            ((["a", "ab"],), "a"),
        ),
    ),
    FamilySpec(
        "rotate_list",
        "validation",
        "Rotate a list right by k, allowing negative k and empty lists; return a new list.",
        """
    def solve(xs,k):
        if not xs:
            return []
        k %= len(xs)
        return xs[-k:] + xs[:-k] if k else list(xs)
    """,
        (
            ("xs[-k:] + xs[:-k]", "xs[k:] + xs[:k]"),
            ("k %= len(xs)", "k %= len(xs)+1"),
            ("if k else list(xs)", "if k else []"),
            ("return []", "return [0]"),
        ),
        (
            (([1, 2, 3, 4], 1), [4, 1, 2, 3]),
            (([1, 2, 3], -1), [2, 3, 1]),
            (([], 3), []),
            (([1, 2], 2), [1, 2]),
            (([1, 2, 3], 7), [3, 1, 2]),
        ),
    ),
    FamilySpec(
        "wildcard_match",
        "validation",
        (
            "Match an entire string against a pattern where ? matches one character and * "
            "matches any sequence, including empty."
        ),
        """
    def solve(text,pattern):
        row = [True] + [False]*len(text)
        for p in pattern:
            nxt = [row[0] and p == '*']
            for j,c in enumerate(text,1):
                nxt.append((nxt[j-1] or row[j]) if p == '*'
                           else (row[j-1] and (p == '?' or p == c)))
            row = nxt
        return row[-1]
    """,
        (
            ("p == '?' or p == c", "p == c"),
            ("nxt[j-1] or row[j]", "nxt[j-1] and row[j]"),
            ("row[0] and p == '*'", "False"),
            ("return row[-1]", "return row[0]"),
        ),
        (
            (("abc", "a?c"), True),
            (("abc", "a*"), True),
            (("", "*"), True),
            (("abc", "ab"), False),
            (("", "?"), False),
            (("abc", "*b*"), True),
        ),
    ),
    FamilySpec(
        "integer_text",
        "test",
        (
            "Parse a possibly signed decimal integer surrounded by whitespace. Empty text, "
            "sign only or non-digit content raises ValueError."
        ),
        """
    def solve(text):
        text = text.strip()
        sign = 1
        if text[:1] in ('+','-'):
            sign = -1 if text[0] == '-' else 1
            text = text[1:]
        if not text or any(c not in '0123456789' for c in text):
            raise ValueError('integer')
        value = 0
        for c in text:
            value = value*10 + '0123456789'.index(c)
        return sign*value
    """,
        (
            ("return sign*value", "return value"),
            ("value*10", "value*9"),
            ("text = text.strip()", "text = text"),
            ("sign = -1 if text[0] == '-' else 1", "sign = 1 if text[0] == '-' else -1"),
        ),
        (((" -42 ",), -42), (("+17",), 17), (("0",), 0), ((" 123 ",), 123), (("-0",), 0)),
        (("",), ("+",), ("12x",), ("1 2",)),
    ),
    FamilySpec(
        "version_compare",
        "test",
        (
            "Compare dotted nonnegative integer version components, ignoring trailing zero "
            "components; return -1,0,1. Inputs are valid."
        ),
        """
    def solve(left,right):
        a = [int(x) for x in left.split('.')]
        b = [int(x) for x in right.split('.')]
        while a and a[-1] == 0:
            a.pop()
        while b and b[-1] == 0:
            b.pop()
        return (a > b) - (a < b)
    """,
        (
            ("(a > b) - (a < b)", "(a < b) - (a > b)"),
            ("while a and", "while False and"),
            ("while b and", "while False and"),
            ("[int(x) for x in left.split('.')]", "left.split('.')"),
        ),
        (
            (("1.2", "1.2.0"), 0),
            (("1.10", "1.2"), 1),
            (("0.0", "0"), 0),
            (("1", "1.0.1"), -1),
            (("1.0.0", "1"), 0),
        ),
    ),
    FamilySpec(
        "radix_encode",
        "test",
        (
            "Encode any integer in base 2 through 16, lowercase digits and optional minus "
            "sign. Invalid base raises ValueError."
        ),
        """
    def solve(n,base):
        if base < 2 or base > 16:
            raise ValueError('base')
        if n == 0:
            return '0'
        sign = '-' if n < 0 else ''
        n, out = abs(n), ''
        while n:
            out = '0123456789abcdef'[n%base] + out
            n //= base
        return sign + out
    """,
        (
            ("return sign + out", "return out"),
            ("'0123456789abcdef'[n%base] + out", "out + '0123456789abcdef'[n%base]"),
            ("return '0'", "return ''"),
            ("base > 16", "base >= 16"),
        ),
        (((31, 16), "1f"), ((-5, 2), "-101"), ((0, 10), "0"), ((10, 2), "1010"), ((12, 3), "110")),
        ((4, 1), (4, 17)),
    ),
    FamilySpec(
        "unescape_text",
        "test",
        (
            "Decode backslash-n, backslash-t and backslash-backslash. Unknown escapes and a "
            "trailing backslash raise ValueError."
        ),
        r"""
    def solve(text):
        mapping = {'n':'\n','t':'\t','\\':'\\'}
        out, i = [], 0
        while i < len(text):
            if text[i] == '\\':
                i += 1
                if i == len(text) or text[i] not in mapping:
                    raise ValueError('escape')
                out.append(mapping[text[i]])
            else:
                out.append(text[i])
            i += 1
        return ''.join(out)
    """,
        (
            (r"'n':'\n'", "'n':'n'"),
            (r"'t':'\t'", "'t':'t'"),
            ("return ''.join(out)", "return ''.join(out[::-1])"),
            ("out.append(text[i])", "out.append('')"),
        ),
        (
            (("a\\nb",), "a\nb"),
            (("x\\ty",), "x\ty"),
            (("a\\\\b",), "a\\b"),
            (("",), ""),
            (("abc",), "abc"),
        ),
        (("x\\",), ("\\q",)),
    ),
    FamilySpec(
        "clock_minutes",
        "test",
        (
            "Parse exactly HH:MM in 24-hour time into minutes since midnight; bad shape or "
            "out-of-range fields raise ValueError."
        ),
        """
    def solve(text):
        if (len(text) != 5 or text[2] != ':'
            or any(c not in '0123456789' for c in text[:2]+text[3:])):
            raise ValueError('shape')
        h,m = int(text[:2]), int(text[3:])
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError('range')
        return h*60+m
    """,
        (
            ("h*60+m", "h*100+m"),
            ("h < 24", "h <= 24"),
            ("m < 60", "m <= 60"),
            ("int(text[3:])", "int(text[:2])"),
        ),
        ((("00:00",), 0), (("12:34",), 754), (("23:59",), 1439), (("01:02",), 62)),
        (("24:00",), ("12:60",), ("1:02",), ("aa:bb",)),
    ),
    FamilySpec(
        "query_pairs",
        "test",
        (
            "Parse ampersand-separated key=value entries; an entry without = has empty value, "
            "empty entries are ignored, repeated keys accumulate values in order. No URL "
            "decoding."
        ),
        """
    def solve(text):
        out = {}
        for item in text.split('&'):
            if not item:
                continue
            pair = item.split('=',1)
            key, value = pair[0], pair[1] if len(pair)==2 else ''
            out.setdefault(key,[]).append(value)
        return out
    """,
        (
            ("split('=',1)", "split('=')"),
            ("out.setdefault(key,[]).append(value)", "out[key] = [value]"),
            ("if not item:", "if item:"),
            ("pair[0], pair[1]", "pair[1], pair[0]"),
        ),
        (
            (("a=1&b=2&a=3",), {"a": ["1", "3"], "b": ["2"]}),
            (("",), {}),
            (("x=a=b&&flag",), {"x": ["a=b"], "flag": [""]}),
            (("=x&a=",), {"": ["x"], "a": [""]}),
        ),
    ),
    FamilySpec(
        "roman_value",
        "test",
        (
            "Convert valid uppercase Roman numerals using subtractive pairs, including empty "
            "string for 0. Inputs use IVXLCDM only."
        ),
        """
    def solve(text):
        values = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}
        total = 0
        for i,c in enumerate(text):
            value = values[c]
            total += -value if i+1 < len(text) and value < values[text[i+1]] else value
        return total
    """,
        (
            ("value < values[text[i+1]]", "value > values[text[i+1]]"),
            ("total = 0", "total = 1"),
            ("return total", "return total % 1000"),
            ("i+1 < len(text)", "i+2 < len(text)"),
        ),
        (
            (("IV",), 4),
            (("IX",), 9),
            (("MCMXCIV",), 1994),
            (("",), 0),
            (("VIII",), 8),
            (("XL",), 40),
        ),
    ),
    FamilySpec(
        "finite_automaton",
        "test",
        (
            "Run deterministic transitions keyed by (state,symbol); return whether final "
            "state is in accepting. Missing transition rejects, including when state or "
            "target is 0."
        ),
        """
    def solve(start,accepting,transitions,symbols):
        state = start
        for symbol in symbols:
            key = (state,symbol)
            if key not in transitions:
                return False
            state = transitions[key]
        return state in accepting
    """,
        (
            ("state = transitions[key]", "state = key[0]"),
            ("return state in accepting", "return state not in accepting"),
            ("if key not in transitions:", "if not transitions.get(key):"),
            ("return False", "return True"),
        ),
        (
            ((0, [1], {(0, "a"): 1}, ["a"]), True),
            ((0, [0], {}, []), True),
            ((0, [0], {(0, "a"): 1}, ["a"]), False),
            ((1, [0], {(1, "a"): 0}, ["a"]), True),
            ((0, [1], {}, ["a"]), False),
        ),
    ),
)


def expansion_tasks() -> tuple[ExpansionTask, ...]:
    """Create exactly four syntax-preserving bug mutations for each private family."""
    result = []
    for family in _FAMILIES:
        reference = dedent(family.source).lstrip("\n")
        if len(family.faults) != VARIANTS_PER_FAMILY:
            raise ValueError("family must declare four faults")
        for variant, (old, new) in enumerate(family.faults):
            if old not in reference:
                raise ValueError("mutation target absent: " + family.name + "/" + str(variant))
            broken = reference.replace(old, new, 1)
            result.append(
                ExpansionTask(
                    family.name,
                    variant,
                    family.split,
                    family.specification + " Do not mutate input arguments.",
                    reference,
                    broken,
                    family.examples,
                    family.invalid,
                )
            )
    return tuple(result)


def verifier_source(task: ExpansionTask, marker: str) -> str:
    """Fixed literal oracles with exceptions and nonmutation; never self-reported success."""
    lines = ["from solution import solve", "from copy import deepcopy"]
    for args, expected in task.examples:
        lines += [
            "args=deepcopy(" + repr(args) + ")",
            "before=deepcopy(args)",
            "actual=solve(*args)",
            "assert type(actual) is type(" + repr(expected) + ")",
            "assert actual == " + repr(expected),
        ]
        if task.preserve_inputs:
            lines += ["assert args == before"]
    for args in task.invalid_inputs:
        lines += [
            "args=deepcopy(" + repr(args) + ")",
            "before=deepcopy(args)",
            "try:",
            "    solve(*args)",
            "except ValueError:",
            "    pass",
            "else:",
            "    raise AssertionError('expected ValueError')",
            "assert args == before",
        ]
    return "\n".join([*lines, "print(" + repr(marker) + ")"]) + "\n"


def public_files(task: ExpansionTask) -> dict[str, str]:
    """Expose one reproducer; exception-only faults still get an executable smoke case."""

    try:
        return public_fixture(task)
    except ValueError as error:
        if str(error) != "task has no visible failing reproducer":
            raise
    namespace: dict[str, object] = {}
    exec(task.broken, namespace)
    solve = cast(Callable[..., object], namespace["solve"])
    for args in task.invalid_inputs:
        try:
            solve(*deepcopy(args))
        except ValueError:
            continue
        except Exception:
            pass
        smoke = (
            "from solution import solve\ntry:\n    solve(*" + repr(args) + ")\n"
            "except ValueError:\n    pass\nelse:\n    raise AssertionError('expected ValueError')\n"
        )
        return {
            "solution.py": "from implementation import solve\n",
            "implementation.py": task.broken,
            "smoke_test.py": smoke,
            "README.md": task.specification + "\nOnly implementation.py may change. "
            "Run python3 smoke_test.py. Pure built-ins only; no imports, I/O or introspection.\n",
        }
    raise ValueError("no normal or exception reproducer found")
