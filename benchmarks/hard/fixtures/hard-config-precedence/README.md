# config

`resolve` is specified in `config/resolve.py`. **The module docstring is the
contract.** `tests/` holds a smoke subset of it and is *not* the criterion -- the task
is judged against the full specification.

Sources are already split for you: `env` and `cli` arrive as mappings of raw strings,
and their keys are already normalised.  Run the smoke subset with
`python -m unittest discover -s .`.
