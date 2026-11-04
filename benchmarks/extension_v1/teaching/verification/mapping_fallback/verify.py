from lesson import lookup

marker = object()
assert lookup({}, "absent") is None
assert lookup({}, "absent", marker) is marker
for value in (None, False, 0, "", [], {}, marker):
    assert lookup({"present": value}, "present", marker) is value
