# ruff: noqa: T201 - executable benchmark assertions and completion marker
from solution import labels

assert labels(["B", "a", "B"]) == ["b", "a"]
print("visible_passed")
