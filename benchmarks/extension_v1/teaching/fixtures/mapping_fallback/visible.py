from lesson import lookup

assert lookup({"present": 2}, "present", 5) == 2
assert lookup({}, "absent", 5) == 5
