# csvkit

`parse_records` is specified in `csvkit/reader.py`. **The module docstring is the
contract.** `tests/` holds a smoke subset of it and is *not* the criterion -- the task
is judged against the full specification.

The standard library `csv` module is available, but the specification below is what
the checks test.  Run the smoke subset with `python -m unittest discover -s .`.
