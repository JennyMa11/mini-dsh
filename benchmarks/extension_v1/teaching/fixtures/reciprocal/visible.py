from lesson import reciprocal

assert reciprocal(2) == 0.5
try:
    reciprocal(0)
except ValueError as error:
    assert str(error) == "value must be nonzero"
else:
    raise AssertionError("zero must raise ValueError")
