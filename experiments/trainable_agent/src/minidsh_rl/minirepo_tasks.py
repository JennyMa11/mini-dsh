"""Frozen development mini-repositories; private checks never enter public files."""

# ruff: noqa: E501 - frozen fixture source literals retain their original lines

import ast
from dataclasses import dataclass
from textwrap import dedent

from minidsh_rl.minirepo_contract_sources import CONTRACTS, validation_source


@dataclass(frozen=True, slots=True)
class MiniTask:
    case_id: str
    family: str
    specification: str
    files: tuple[tuple[str, str], ...]
    writable: tuple[str, ...]
    reference: tuple[tuple[str, str], ...]
    hidden_checks: str


def _make(  # noqa: PLR0913, PLR0917 - immutable task boundary
    family: str,
    variant: int,
    specification: str,
    modules: dict[str, str],
    faults: tuple[tuple[str, str, str], ...],
    public: str,
    hidden: str,
) -> MiniTask:
    reference = {name: dedent(source).strip() + "\n" for name, source in modules.items()}
    reference["contracts.py"] = CONTRACTS
    reference["validation.py"] = validation_source(family)
    service = reference["service.py"]
    tree = ast.parse(service)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "solve")
    position = function.body[1].lineno - 1
    lines = service.splitlines(keepends=True)
    lines.insert(position, "    validate_request(payload)\n")
    reference["service.py"] = "from validation import validate_request\n" + "".join(lines)
    specification += (
        " Invalid top-level payloads (non-dict or missing required fields) "
        "raise ValueError; preserve the existing validation contract."
    )
    hidden = (
        dedent(hidden).strip()
        + "\nfor bad in (None, {}, []):\n    try:\n        solve(bad)\n    except ValueError:\n        pass\n    else:\n        raise AssertionError('missing validation')\n"
    )
    broken = dict(reference)
    for name, old, new in faults:
        if broken[name].count(old) != 1:
            raise ValueError("fault must identify exactly one source location")
        broken[name] = broken[name].replace(old, new, 1)
    visible = (
        "from service import solve\n\n"
        + dedent(public).strip()
        + "\nprint('public tests passed')\n"
    )
    files = broken | {"public_tests.py": visible, "README.md": specification}
    return MiniTask(
        f"mini_{family}_{variant}",
        family,
        specification,
        tuple(sorted(files.items())),
        tuple(sorted(reference)),
        tuple(sorted(reference.items())),
        dedent(hidden).strip() + "\n",
    )


def _inventory(variant: int) -> MiniTask:
    return _make(
        "inventory",
        variant,
        "Repair inventory reservations. solve(payload) takes stock: sku->nonnegative integer and "
        "orders: [[sku, positive_quantity],...]. Combine duplicate orders by addition. Accept only "
        "if every combined quantity is available. On acceptance subtract quantities; on failure "
        "leave stock unchanged. Return {accepted: bool, remaining: dict, shortages: dict}; "
        "shortages maps each insufficient SKU to requested minus available. Missing stock is 0. "
        "Do not mutate either input, including on failure. Empty orders succeed. "
        "Keep existing APIs. Use read/search/patch/test tools to inspect and repair the repository.",
        {
            "orders.py": '''
                def combine_orders(orders):
                    """Aggregate repeated SKU requests without altering the original rows.

                    Ordering is irrelevant to reservation totals. Every incoming quantity
                    is a positive integer; validation belongs to the caller's API boundary.
                    A new dictionary is returned for every request, including empty ones.
                    """
                    totals = {}
                    for sku, quantity in orders:
                        totals[sku] = totals.get(sku, 0) + quantity
                    return totals

                def requested_skus(orders):
                    """Expose unique requested identifiers to repository callers."""
                    return sorted(combine_orders(orders))
            ''',
            "stock.py": '''
                def find_shortages(stock, totals):
                    """Describe every unavailable quantity, not the entire request.

                    A missing product has zero availability. Exact depletion is allowed.
                    This function is a read-only planning operation: callers may show its
                    result before deciding whether to commit a reservation.
                    """
                    shortages = {}
                    for sku, quantity in totals.items():
                        available = stock.get(sku, 0)
                        if available < quantity:
                            shortages[sku] = quantity - available
                    return shortages

                def available_units(stock):
                    """Return the total units represented by a stock snapshot."""
                    return sum(stock.values())
            ''',
            "reservation.py": '''
                from orders import combine_orders
                from stock import find_shortages

                def reserve(stock, orders):
                    """Plan a whole request and apply it only when all items fit.

                    Consumers retain ownership of stock. Even a successful reservation
                    returns a separate mapping so that retrying against the original
                    snapshot has no dependency on an earlier request.
                    """
                    totals = combine_orders(orders)
                    shortages = find_shortages(stock, totals)
                    remaining = dict(stock)
                    if shortages:
                        return False, remaining, shortages
                    for sku, quantity in totals.items():
                        remaining[sku] = remaining.get(sku, 0) - quantity
                    return True, remaining, {}
            ''',
            "service.py": '''
                from reservation import reserve

                def solve(payload):
                    """Reserve one request and expose the stable application response.

                    The caller may reuse payload for a later retry, so downstream code
                    must treat its mappings and row lists as borrowed values.
                    Failure is a normal domain result rather than a Python exception.
                    """
                    accepted, remaining, shortages = reserve(payload['stock'], payload['orders'])
                    return {'accepted': accepted, 'remaining': remaining, 'shortages': shortages}
            ''',
        },
        (
            ("orders.py", "totals.get(sku, 0) + quantity", "quantity"),
            ("reservation.py", "remaining = dict(stock)", "remaining = stock"),
        )
        if variant == 0
        else (
            ("stock.py", "available < quantity", "available <= quantity"),
            ("reservation.py", "remaining = dict(stock)", "remaining = stock"),
        ),
        """
        payload = {'stock': {'a': 5}, 'orders': [['a', 2], ['a', 2]]}
        assert solve(payload) == {'accepted': True, 'remaining': {'a': 1}, 'shortages': {}}
        assert payload['stock'] == {'a': 5}
        assert solve({'stock': {'a': 2}, 'orders': [['a', 2]]}) == {
            'accepted': True, 'remaining': {'a': 0}, 'shortages': {}}
        """,
        """
        assert solve({'stock': {}, 'orders': []}) == {'accepted': True, 'remaining': {}, 'shortages': {}}
        assert solve({'stock': {}, 'orders': [['z', 3]]}) == {
            'accepted': False, 'remaining': {}, 'shortages': {'z': 3}}
        p = {'stock': {'x': 4, 'y': 1}, 'orders': [['x', 3], ['x', 3], ['y', 2]]}
        assert solve(p) == {'accepted': False, 'remaining': {'x': 4, 'y': 1}, 'shortages': {'x': 2, 'y': 1}}
        assert p == {'stock': {'x': 4, 'y': 1}, 'orders': [['x', 3], ['x', 3], ['y', 2]]}
        p = {'stock': {'q': 7}, 'orders': [['q', 1]]}
        assert solve(p)['remaining'] == {'q': 6}
        assert solve(p)['remaining'] == {'q': 6}
        assert p['stock'] == {'q': 7}
        assert solve({'stock': {'k': 3}, 'orders': [['k', 1], ['k', 2]]}) == {
            'accepted': True, 'remaining': {'k': 0}, 'shortages': {}}
        """,
    )


def _windows(variant: int) -> MiniTask:
    return _make(
        "windows",
        variant,
        "Repair event-window reporting. solve(payload) takes events: [[id,time,channel],...], "
        "start and end integers. Include start <= time < end; among included events with the "
        "same id retain the largest time (last input wins ties). Normalize channel using strip "
        "and lower, then count retained events per channel. Return {counts: dict, total: int}. "
        "Input may be unsorted. Filter before deduplication. Do not mutate input. Preserve APIs.",
        {
            "records.py": '''
                def normalize(event):
                    """Convert a borrowed event row into a normalized immutable record.

                    The record schema is identifier, integer timestamp, and channel.
                    Identifiers are case-sensitive; channel labels are not.
                    Whitespace at the boundary of a channel is insignificant.
                    """
                    identifier, timestamp, channel = event
                    return identifier, timestamp, channel.strip().lower()

                def normalize_all(events):
                    """Create fresh records while preserving input arrival order."""
                    return [normalize(event) for event in events]
            ''',
            "window.py": '''
                def in_window(timestamp, start, end):
                    """Use half-open windows so adjacent reports never double-count.

                    Start and end are supplied by the caller. An empty or reversed
                    interval has no members. Integer timestamps may be negative.
                    Boundary handling is shared by direct callers and report building.
                    """
                    return start <= timestamp < end

                def select(records, start, end):
                    """Filter first: a newer event outside the window is irrelevant."""
                    return [row for row in records if in_window(row[1], start, end)]
            ''',
            "aggregate.py": '''
                def latest_by_id(records):
                    """Retain the newest in-window event for every identifier.

                    Time order and arrival order need not agree. Equal timestamps
                    are deliberately resolved by the last input occurrence.
                    Returning a new list avoids ownership of the caller's container.
                    """
                    chosen = {}
                    for row in records:
                        key = row[0]
                        if key not in chosen or row[1] >= chosen[key][1]:
                            chosen[key] = row
                    return list(chosen.values())

                def count_channels(records):
                    """Count selected records; no channel appears with a zero count."""
                    counts = {}
                    for row in records:
                        counts[row[2]] = counts.get(row[2], 0) + 1
                    return counts
            ''',
            "service.py": '''
                from records import normalize_all
                from window import select
                from aggregate import latest_by_id, count_channels

                def solve(payload):
                    """Build a window report through the shared repository helpers.

                    The public response reports distinct retained events, not raw
                    input rows. No intermediate operation may modify the event list.
                    """
                    records = normalize_all(payload['events'])
                    selected = select(records, payload['start'], payload['end'])
                    latest = latest_by_id(selected)
                    return {'counts': count_channels(latest), 'total': len(latest)}
            ''',
        },
        (
            ("window.py", "start <= timestamp < end", "start <= timestamp <= end"),
            ("aggregate.py", "key not in chosen or row[1] >= chosen[key][1]", "key not in chosen"),
        )
        if variant == 0
        else (
            ("window.py", "start <= timestamp < end", "start < timestamp < end"),
            ("records.py", "channel.strip().lower()", "channel.lower()"),
        ),
        """
        p = {'events': [['a', 0, ' X '], ['a', 2, 'Y'], ['b', 5, 'X']], 'start': 0, 'end': 5}
        assert solve(p) == {'counts': {'y': 1}, 'total': 1}
        assert solve({'events': [['a', 0, ' X ']], 'start': 0, 'end': 1}) == {
            'counts': {'x': 1}, 'total': 1}
        """,
        """
        assert solve({'events': [], 'start': 0, 'end': 1}) == {'counts': {}, 'total': 0}
        p = {'events': [['x', 4, 'A'], ['x', 1, 'B'], ['x', 4, ' C '], ['x', 9, 'Z']], 'start': 0, 'end': 5}
        assert solve(p) == {'counts': {'c': 1}, 'total': 1}
        assert p['events'] == [['x', 4, 'A'], ['x', 1, 'B'], ['x', 4, ' C '], ['x', 9, 'Z']]
        assert solve({'events': [['z', -2, ' Q ']], 'start': -2, 'end': -1}) == {
            'counts': {'q': 1}, 'total': 1}
        assert solve({'events': [['z', 2, 'q']], 'start': 2, 'end': 2}) == {'counts': {}, 'total': 0}
        """,
    )


def _settings(variant: int) -> MiniTask:
    return _make(
        "settings",
        variant,
        "Repair layered configuration. solve(payload) takes base and override dictionaries plus "
        "paths: dotted keys. Merge recursively only when both values are dicts; otherwise "
        "override replaces, including 0, False, empty list/string and None. Lists replace rather "
        "than append. Return {config: merged, values: [lookup each path]}; a missing key returns "
        "'MISSING', but an explicitly present falsey value is preserved. Neither input may mutate "
        "or share nested mutable objects with the returned configuration. Preserve existing APIs.",
        {
            "values.py": '''
                def clone(value):
                    """Copy nested JSON-like containers without external dependencies.

                    Immutable scalar values may be reused. Dictionaries and lists
                    must be new objects at every depth, including inside a replaced
                    list. The service accepts acyclic configuration values only.
                    """
                    if isinstance(value, dict):
                        return {key: clone(item) for key, item in value.items()}
                    if isinstance(value, list):
                        return [clone(item) for item in value]
                    return value
            ''',
            "merge.py": '''
                from values import clone

                def merge(base, override):
                    """Apply a layer while keeping the caller's snapshots reusable.

                    Nested dictionaries merge recursively, whereas list and scalar
                    values replace. A key's existence is independent of truthiness:
                    callers intentionally use zero, False and None as configuration.
                    """
                    result = clone(base)
                    for key, value in override.items():
                        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                            result[key] = merge(result[key], value)
                        else:
                            result[key] = clone(value)
                    return result
            ''',
            "lookup.py": '''
                def lookup(config, path):
                    """Resolve dotted paths with a sentinel only for absent values.

                    A scalar cannot be traversed further. None is an explicit value,
                    and must not be mistaken for a missing node. The path syntax
                    addresses dictionary keys rather than list positions.
                    """
                    node = config
                    for key in path.split('.'):
                        if not isinstance(node, dict) or key not in node:
                            return 'MISSING'
                        node = node[key]
                    return node
            ''',
            "service.py": '''
                from merge import merge
                from lookup import lookup

                def solve(payload):
                    """Resolve a configuration layer and project requested settings.

                    Both source layers remain available for later service calls.
                    Returned mutable values belong to this response, rather than
                    the original request held by another component.
                    """
                    config = merge(payload['base'], payload['override'])
                    return {'config': config, 'values': [lookup(config, p) for p in payload['paths']]}
            ''',
        },
        (
            ("merge.py", "result[key] = merge(result[key], value)", "result[key] = clone(value)"),
            ("lookup.py", "return node\n", "return node or 'MISSING'\n"),
        )
        if variant == 0
        else (
            ("merge.py", "result = clone(base)", "result = base"),
            ("values.py", "return [clone(item) for item in value]", "return value"),
        ),
        """
        p = {'base': {'db': {'port': 8, 'host': 'local'}}, 'override': {'db': {'port': 0}}, 'paths': ['db.port', 'db.host']}
        assert solve(p) == {'config': {'db': {'port': 0, 'host': 'local'}}, 'values': [0, 'local']}
        assert p['base'] == {'db': {'port': 8, 'host': 'local'}}
        p = {'base': {'x': [1]}, 'override': {}, 'paths': ['x']}
        r = solve(p)
        r['config']['x'].append(2)
        assert p['base'] == {'x': [1]}
        """,
        """
        p = {'base': {'a': {'b': 1}, 'xs': [1]}, 'override': {'a': None, 'xs': []}, 'paths': ['a', 'a.b', 'xs', 'no']}
        assert solve(p) == {'config': {'a': None, 'xs': []}, 'values': [None, 'MISSING', [], 'MISSING']}
        assert p['base'] == {'a': {'b': 1}, 'xs': [1]}
        p = {'base': {}, 'override': {'z': [{'v': False}]}, 'paths': ['z']}
        r = solve(p)
        r['config']['z'][0]['v'] = True
        assert p['override'] == {'z': [{'v': False}]}
        assert solve({'base': {'v': 1}, 'override': {'v': ''}, 'paths': ['v']})['values'] == ['']
        assert solve({'base': {'a': {'x': 1, 'y': 2}}, 'override': {'a': {'x': 3}}, 'paths': ['a.y']}) == {
            'config': {'a': {'x': 3, 'y': 2}}, 'values': [2]}
        """,
    )


def _billing(variant: int) -> MiniTask:
    return _make(
        "billing",
        variant,
        "Repair invoice arithmetic using integer cents. solve(payload) takes lines, each with "
        "sku, price, quantity, discount (integer percent 0..100), plus tax_rate (integer percent). "
        "Discount each unit first with floor(price*(100-discount)/100), then multiply by quantity. "
        "Group repeated SKUs by summing line totals. Tax is ceiling(net*tax_rate/100), once per "
        "invoice. Return {by_sku: dict, net: int, tax: int, total: int}. Empty lines produce all "
        "zero amounts. Preserve inputs and repository APIs.",
        {
            "money.py": '''
                def discounted_unit(price, discount):
                    """Round the discounted per-unit charge down to whole cents.

                    The business contract rounds units before multiplying quantity.
                    Doing these operations in the opposite order can move the final
                    invoice by a cent for fractional percentage discounts.
                    """
                    return price * (100 - discount) // 100

                def invoice_tax(net, rate):
                    """Round the one invoice-level tax up to the nearest cent."""
                    return (net * rate + 99) // 100
            ''',
            "lines.py": '''
                from money import discounted_unit

                def line_total(line):
                    """Apply unit pricing and quantity for a borrowed invoice row.

                    All fields are already validated at the API boundary. Invoices
                    may contain repeated SKUs and differently discounted lines.
                    This calculation does not merge or modify those input rows.
                    """
                    unit = discounted_unit(line['price'], line['discount'])
                    return unit * line['quantity']

                def line_sku(line):
                    """Keep identifiers exactly as supplied, including case."""
                    return line['sku']
            ''',
            "totals.py": '''
                from lines import line_total, line_sku

                def totals_by_sku(lines):
                    """Combine monetary amounts, keeping no references to rows.

                    Different lines can have the same identifier and different
                    unit prices. Aggregation happens after pricing each line.
                    Empty invoices return an empty mapping rather than a sentinel.
                    """
                    amounts = {}
                    for line in lines:
                        sku = line_sku(line)
                        amounts[sku] = amounts.get(sku, 0) + line_total(line)
                    return amounts
            ''',
            "service.py": '''
                from totals import totals_by_sku
                from money import invoice_tax

                def solve(payload):
                    """Price an invoice with stable integer-valued response fields.

                    Tax is calculated once, after grouping all line amounts.
                    Rounding an independent tax for each line would produce a
                    different result and is outside this invoice contract.
                    """
                    by_sku = totals_by_sku(payload['lines'])
                    net = sum(by_sku.values())
                    tax = invoice_tax(net, payload['tax_rate'])
                    return {'by_sku': by_sku, 'net': net, 'tax': tax, 'total': net + tax}
            ''',
        },
        (
            ("money.py", "net * rate + 99", "net * rate"),
            ("totals.py", "amounts.get(sku, 0) + line_total(line)", "line_total(line)"),
        )
        if variant == 0
        else (
            ("lines.py", "return unit * line['quantity']", "return unit"),
            ("money.py", "100 - discount", "100 + discount"),
        ),
        """
        p = {'lines': [{'sku': 'a', 'price': 101, 'quantity': 2, 'discount': 10},
                       {'sku': 'a', 'price': 1, 'quantity': 1, 'discount': 0}], 'tax_rate': 7}
        assert solve(p) == {'by_sku': {'a': 181}, 'net': 181, 'tax': 13, 'total': 194}
        assert p['lines'][0]['price'] == 101
        """,
        """
        assert solve({'lines': [], 'tax_rate': 17}) == {'by_sku': {}, 'net': 0, 'tax': 0, 'total': 0}
        p = {'lines': [{'sku': 'z', 'price': 9, 'quantity': 4, 'discount': 100}], 'tax_rate': 20}
        assert solve(p) == {'by_sku': {'z': 0}, 'net': 0, 'tax': 0, 'total': 0}
        p = {'lines': [{'sku': 'a', 'price': 1, 'quantity': 1, 'discount': 0},
                       {'sku': 'b', 'price': 1, 'quantity': 1, 'discount': 0}], 'tax_rate': 1}
        assert solve(p) == {'by_sku': {'a': 1, 'b': 1}, 'net': 2, 'tax': 1, 'total': 3}
        assert p['lines'][1]['quantity'] == 1
        p = {'lines': [{'sku': 'r', 'price': 5, 'quantity': 3, 'discount': 20},
                       {'sku': 'r', 'price': 7, 'quantity': 2, 'discount': 0}], 'tax_rate': 0}
        assert solve(p) == {'by_sku': {'r': 26}, 'net': 26, 'tax': 0, 'total': 26}
        """,
    )


def development_tasks() -> tuple[MiniTask, ...]:
    """Four new development-only families; no training or held-out test claimed."""
    return tuple(
        builder(variant)
        for builder in (_inventory, _windows, _settings, _billing)
        for variant in range(2)
    )
