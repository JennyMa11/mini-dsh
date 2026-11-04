# ruff: noqa: T201 - executable benchmark assertions and completion marker
from solution import labels

assert labels(["B", "a", "B"]) == ["b", "a"]
assert labels([" STRASSE ", "Straße", "", "  ", "C"]) == ["strasse", "c"]
assert labels([]) == []
assert labels(["Z", "A", "z"]) == ["z", "a"]
print("independent_passed")
