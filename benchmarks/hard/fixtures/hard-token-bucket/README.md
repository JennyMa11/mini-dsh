# ratelimit

`TokenBucket` is specified in `ratelimit/bucket.py`. **The module docstring is the
contract.** `tests/` holds a smoke subset of it and is *not* the criterion -- the task
is judged against the full specification.

The clock is injected, so the tests never sleep: they advance a counter and call the
bucket again.  Run the smoke subset with `python -m unittest discover -s .`.
