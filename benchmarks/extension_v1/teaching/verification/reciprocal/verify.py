from lesson import reciprocal

for value, expected in [(1, 1), (2, 0.5), (-4, -0.25), (0.25, 4)]:
    assert reciprocal(value) == expected
for value in (0, 0.0, -0.0):
    try:
        reciprocal(value)
    except ValueError as error:
        assert str(error) == "value must be nonzero"
    else:
        raise AssertionError("zero precondition was not enforced")
