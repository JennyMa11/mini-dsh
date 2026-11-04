# ruff: noqa: T201, PLR2004 - executable benchmark assertions and completion marker
from solution import total

assert total([2, 3]) == 5
assert total([]) == 0
assert total([-3, 1, 7]) == 5
assert total([0.5, 1.5]) == 2
print("independent_passed")
