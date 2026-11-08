"""Public project validation code shared by frozen mini-repository builders."""

from textwrap import dedent

CONTRACTS = (
    dedent('''
def require_mapping(value, name):
    """Require a mapping without copying or changing ownership."""
    if not isinstance(value, dict):
        raise ValueError(name)
    return value

def require_list(value, name):
    """Inputs use JSON arrays rather than arbitrary lazy iterators."""
    if not isinstance(value, list):
        raise ValueError(name)
    return value

def require_keys(value, keys):
    """Check required fields while allowing unrelated metadata."""
    require_mapping(value, 'request must be a mapping')
    for key in keys:
        if key not in value:
            raise ValueError('missing field: ' + key)
    return value

def require_integer(value, name, minimum=None, maximum=None):
    """Booleans are distinct from quantities, despite Python's int subclass."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(name)
    if minimum is not None and value < minimum:
        raise ValueError(name)
    if maximum is not None and value > maximum:
        raise ValueError(name)
    return value

def require_text(value, name, nonempty=False):
    """Keep caller spelling; normalization belongs to domain operations."""
    if not isinstance(value, str):
        raise ValueError(name)
    if nonempty and not value:
        raise ValueError(name)
    return value

def require_row(value, width, name):
    """Reject malformed records before unpacking them downstream."""
    require_list(value, name)
    if len(value) != width:
        raise ValueError(name)
    return value
''').strip()
    + "\n"
)


def validation_source(family: str) -> str:
    """Return real request validation used by the public service entry point."""
    header = (
        "from contracts import require_mapping, require_list, require_keys\n"
        "from contracts import require_integer, require_text, require_row\n\n"
    )
    common = '''
    def validate_request(payload):
        """Check the service boundary before invoking domain calculations.

        Validation never modifies requests. Domain helpers can therefore rely on
        valid row shapes and scalar types while remaining independent of transport.
        All malformed input errors use ValueError as the service API contract.
        This module is also consumed by callers that validate before queuing work.
        """
    '''
    bodies = {
        "inventory": """
        require_keys(payload, ['stock', 'orders'])
        stock = require_mapping(payload['stock'], 'stock')
        orders = require_list(payload['orders'], 'orders')
        for sku, quantity in stock.items():
            require_text(sku, 'sku', True)
            require_integer(quantity, 'stock quantity', 0)
        for row in orders:
            require_row(row, 2, 'order')
            require_text(row[0], 'sku', True)
            require_integer(row[1], 'requested quantity', 1)
        return payload
        """,
        "windows": """
        require_keys(payload, ['events', 'start', 'end'])
        require_integer(payload['start'], 'start')
        require_integer(payload['end'], 'end')
        events = require_list(payload['events'], 'events')
        for row in events:
            require_row(row, 3, 'event')
            require_text(row[0], 'identifier', True)
            require_integer(row[1], 'timestamp')
            require_text(row[2], 'channel')
        return payload
        """,
        "billing": """
        require_keys(payload, ['lines', 'tax_rate'])
        require_integer(payload['tax_rate'], 'tax_rate', 0, 100)
        lines = require_list(payload['lines'], 'lines')
        for row in lines:
            require_keys(row, ['sku', 'price', 'quantity', 'discount'])
            require_text(row['sku'], 'sku', True)
            require_integer(row['price'], 'price', 0)
            require_integer(row['quantity'], 'quantity', 0)
            require_integer(row['discount'], 'discount', 0, 100)
        return payload
        """,
        "settings": """
        require_keys(payload, ['base', 'override', 'paths'])
        require_mapping(payload['base'], 'base')
        require_mapping(payload['override'], 'override')
        check_value(payload['base'])
        check_value(payload['override'])
        paths = require_list(payload['paths'], 'paths')
        for path in paths:
            require_text(path, 'path', True)
        return payload
        """,
    }
    footer = '''
def check_value(value):
    """Check acyclic JSON-like nested values without retaining aliases."""
    if isinstance(value, dict):
        for key, item in value.items():
            require_text(key, 'configuration key')
            check_value(item)
    elif isinstance(value, list):
        for item in value:
            check_value(item)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise ValueError('unsupported configuration value')
'''
    footer += "\n" + dedent("""
    def validate_many(payloads):
        '''Validate a batch while keeping every original request untouched.

        The returned list is a new container. Its members remain borrowed request
        mappings, matching validate_request rather than cloning domain inputs.
        The caller must wait for this function to return before committing work.
        '''
        require_list(payloads, 'requests')
        checked = []
        for payload in payloads:
            checked.append(validate_request(payload))
        return checked

    def is_valid(payload):
        '''Provide a boolean admission probe for callers that do not use exceptions.

        Domain computation is deliberately absent: this checks shape and type,
        not whether a reservation succeeds or an invoice has a nonzero amount.
        '''
        try:
            validate_request(payload)
        except ValueError:
            return False
        return True
    """)
    return (
        header
        + dedent(common).strip()
        + "\n"
        + "\n".join(
            "    " + line if line else "" for line in dedent(bodies[family]).strip().splitlines()
        )
        + "\n"
        + footer
    )
