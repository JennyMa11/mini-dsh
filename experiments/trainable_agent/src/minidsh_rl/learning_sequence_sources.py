"""Sequence, selection and expression fixtures; split metadata is authoritative."""

# ruff: noqa: E501 - immutable fixture source literals retain complete lines

from minidsh_rl.learning_contracts import FamilyDefinition, Mutation


def aliases() -> FamilyDefinition:
    missing_link = Mutation(
        "links.py", "return aliases[name]", "return None", "follow alias targets"
    )
    self_link = Mutation(
        "links.py", "return aliases[name]", "return name", "follow target rather than self"
    )
    missing = Mutation(
        "errors.py",
        "return {'error': 'missing'}",
        "return {'error': 'cycle'}",
        "missing and cycle are distinct",
    )
    return FamilyDefinition(
        "alias_resolution",
        "test",
        "entities maps names to values; aliases maps names to names; queries is an ordered list of names. "
        "Resolve each query along arbitrarily many alias links to an entity value. Entities take precedence "
        "over aliases with the same name. Missing target returns {error:'missing'}, repeated name within "
        "a single chain returns {error:'cycle'}. Shared tails across queries are not cycles. Return ordered "
        "results without modifying any input. Names and aliases are strings.",
        (
            (
                "links.py",
                """
                def next_name(aliases, name):
                    return aliases[name]
            """,
            ),
            (
                "errors.py",
                """
                def missing_error():
                    return {'error': 'missing'}

                def cycle_error():
                    return {'error': 'cycle'}
            """,
            ),
            (
                "resolution.py",
                """
                from links import next_name
                from errors import missing_error, cycle_error

                def resolve(entities, aliases, name):
                    seen = set()
                    while True:
                        if name in entities:
                            return entities[name]
                        if name in seen:
                            return cycle_error()
                        if name not in aliases:
                            return missing_error()
                        seen.add(name)
                        name = next_name(aliases, name)
            """,
            ),
        ),
        "from resolution import resolve",
        "[resolve(payload['entities'], payload['aliases'], name) for name in payload['queries']]",
        ("entities", "aliases", "queries"),
        ((missing_link, missing), (self_link, missing)),
        (
            Mutation(
                "resolution.py",
                "if name in entities:",
                "if name in entities and name not in aliases:",
                "entity precedence over alias",
            ),
            Mutation(
                "service.py",
                "for name in payload['queries']",
                "for name in reversed(payload['queries'])",
                "query order preserved",
            ),
        ),
        """
        p = {'entities':{'target':7, 'same':9}, 'aliases':{'a':'b','b':'target','same':'target','c':'d','d':'c'}, 'queries':['a','b','absent','c','same']}
        assert solve(p) == [7,7,{'error':'missing'},{'error':'cycle'},9], 'contract: chain/errors/precedence/order'
        assert solve({'entities':{}, 'aliases':{}, 'queries':[]}) == [], 'contract: empty'
        """,
        """
        p = {'entities':{'end':'ok','both':'own'}, 'aliases':{'left':'tail','right':'tail','tail':'end','both':'end','lost':'unknown','loop':'loop'}, 'queries':['both','right','left','lost','loop']}
        assert solve(p) == ['own','ok','ok',{'error':'missing'},{'error':'cycle'}]
        assert p['aliases']['both'] == 'end'
        assert solve({'entities':{'n':None}, 'aliases':{}, 'queries':['n']}) == [None]
        """,
    )


def packing() -> FamilyDefinition:
    equality = Mutation(
        "capacity.py", "weight <= remaining", "weight < remaining", "exact fit is accepted"
    )
    direction = Mutation(
        "capacity.py",
        "weight <= remaining",
        "weight >= remaining",
        "fit must not exceed remaining capacity",
    )
    first_fit = Mutation(
        "selection.py",
        "for i in range(len(remaining)):",
        "for i in reversed(range(len(remaining))):",
        "earliest fitting bin",
    )
    return FamilyDefinition(
        "greedy_packing",
        "test",
        "capacity is a positive integer, weights are integers 1..capacity; invalid capacity/weight raises "
        "ValueError. Process weights in original order. Put each into the earliest existing bin with "
        "enough remaining capacity (equality allowed), or open a new bin. Return bins as lists of weights "
        "in placement order. Empty weights returns []. Do not sort or mutate inputs.",
        (
            (
                "capacity.py",
                """
                def fits(weight, remaining):
                    return weight <= remaining
            """,
            ),
            (
                "selection.py",
                """
                from capacity import fits

                def first_bin(remaining, weight):
                    for i in range(len(remaining)):
                        if fits(weight, remaining[i]):
                            return i
                    return None
            """,
            ),
            (
                "packing.py",
                """
                from selection import first_bin

                def pack(weights, capacity):
                    if capacity <= 0:
                        raise ValueError('capacity')
                    remaining = []
                    bins = []
                    for weight in weights:
                        if weight <= 0 or weight > capacity:
                            raise ValueError('weight')
                        index = first_bin(remaining, weight)
                        if index is None:
                            bins.append([weight])
                            remaining.append(capacity - weight)
                        else:
                            bins[index].append(weight)
                            remaining[index] -= weight
                    return bins
            """,
            ),
        ),
        "from packing import pack",
        "pack(payload['weights'], payload['capacity'])",
        ("weights", "capacity"),
        ((equality, first_fit), (direction, first_fit)),
        (
            Mutation(
                "packing.py",
                "for weight in weights:",
                "for weight in sorted(weights, reverse=True):",
                "do not reorder input",
            ),
            Mutation(
                "packing.py", "return bins", "return bins or [[]]", "empty weights yield no bins"
            ),
        ),
        """
        assert solve({'weights':[6,6,2], 'capacity':10}) == [[6,2],[6]], 'contract: first fit'
        assert solve({'weights':[4,6], 'capacity':10}) == [[4,6]], 'contract: equal capacity'
        assert solve({'weights':[2,8,1], 'capacity':10}) == [[2,8],[1]], 'contract: arrival order'
        assert solve({'weights':[], 'capacity':5}) == [], 'contract: empty'
        """,
        """
        p = {'weights':[5,5,1,1,2], 'capacity':8}
        assert solve(p) == [[5,1,1],[5,2]]
        assert p['weights'] == [5,5,1,1,2]
        assert solve({'weights':[3,4], 'capacity':7}) == [[3,4]]
        assert solve({'weights':[], 'capacity':3}) == []
        for bad in ({'weights':[0], 'capacity':4}, {'weights':[5], 'capacity':4}, {'weights':[], 'capacity':0}):
            try:
                solve(bad)
            except ValueError:
                pass
            else:
                raise AssertionError('invalid packing input')
        """,
    )


def media() -> FamilyDefinition:
    wildcard = Mutation(
        "media.py",
        "return (major == '*' or major == actual_major) and (minor == '*' or minor == actual_minor)",
        "return pattern == offer",
        "media wildcards match offers",
    )
    major = Mutation(
        "media.py",
        "return (major == '*' or major == actual_major) and (minor == '*' or minor == actual_minor)",
        "return minor == '*' or minor == actual_minor",
        "major type must also match",
    )
    unacceptable = Mutation(
        "negotiation.py", "if quality <= 0:", "if quality < 0:", "zero quality is unacceptable"
    )
    return FamilyDefinition(
        "media_negotiation",
        "train",
        "offers is an ordered list of lowercase type/subtype strings. accept is ordered [pattern,q] pairs "
        "where q is integer 0..1000 and pattern is exact type/subtype, type/* or */*. For each offer choose "
        "the matching accept pattern of greatest specificity (exact > type/* > */*), ties earliest accept. "
        "Its q determines quality; no match or q=0 rejects that offer. Select highest quality, then "
        "specificity, then earliest offer. Return its string or None. Never mutate input.",
        (
            (
                "media.py",
                """
                def matches(pattern, offer):
                    major, minor = pattern.split('/')
                    actual_major, actual_minor = offer.split('/')
                    return (major == '*' or major == actual_major) and (minor == '*' or minor == actual_minor)

                def specificity(pattern):
                    if pattern == '*/*':
                        return 0
                    return 1 if pattern.endswith('/*') else 2
            """,
            ),
            (
                "preferences.py",
                """
                from media import matches, specificity

                def offer_quality(offer, accept):
                    selected = None
                    result = (0, -1)
                    for order, row in enumerate(accept):
                        pattern, quality = row
                        if matches(pattern, offer):
                            score = specificity(pattern), -order
                            if selected is None or score > selected:
                                selected = score
                                result = quality, specificity(pattern)
                    return result
            """,
            ),
            (
                "negotiation.py",
                """
                from preferences import offer_quality

                def negotiate(offers, accept):
                    selected = None
                    result = None
                    for order, offer in enumerate(offers):
                        quality, rank = offer_quality(offer, accept)
                        if quality <= 0:
                            continue
                        score = quality, rank, -order
                        if selected is None or score > selected:
                            selected = score
                            result = offer
                    return result
            """,
            ),
        ),
        "from negotiation import negotiate",
        "negotiate(payload['offers'], payload['accept'])",
        ("offers", "accept"),
        ((wildcard, unacceptable), (major, unacceptable)),
        (
            Mutation(
                "preferences.py",
                "score = specificity(pattern), -order",
                "score = quality, -order",
                "specific rejection beats broad acceptance",
            ),
            Mutation(
                "negotiation.py",
                "score = quality, rank, -order",
                "score = quality, rank, order",
                "earliest equally scored offer",
            ),
        ),
        """
        assert solve({'offers':['text/plain'], 'accept':[['text/*',700]]}) == 'text/plain', 'contract: wildcard'
        assert solve({'offers':['text/plain'], 'accept':[['image/*',900]]}) is None, 'contract: major type'
        assert solve({'offers':['text/plain'], 'accept':[['*/*',1000],['text/plain',0]]}) is None, 'contract: specific q0'
        assert solve({'offers':['text/plain','text/html'], 'accept':[['text/*',500]]}) == 'text/plain', 'contract: offer order'
        assert solve({'offers':['text/plain'], 'accept':[['text/plain',0]]}) is None, 'contract: unacceptable'
        """,
        """
        p = {'offers':['app/json','app/xml'], 'accept':[['*/*',300],['app/*',600]]}
        assert solve(p) == 'app/json'
        assert p['offers'] == ['app/json','app/xml']
        assert solve({'offers':['app/json'], 'accept':[['*/*',800],['app/json',0]]}) is None
        assert solve({'offers':['app/json'], 'accept':[['video/*',900]]}) is None
        assert solve({'offers':[], 'accept':[['*/*',1000]]}) is None
        assert solve({'offers':['a/b'], 'accept':[['a/b',200],['a/b',900]]}) == 'a/b'
        """,
    )


def expressions() -> FamilyDefinition:
    precedence = Mutation(
        "operations.py",
        "return 2 if token == '*' else 1",
        "return 1",
        "multiplication before addition",
    )
    low_precedence = Mutation(
        "operations.py",
        "return 2 if token == '*' else 1",
        "return 0 if token == '*' else 1",
        "multiplication not below addition",
    )
    associativity = Mutation(
        "parsing.py",
        "precedence(operator) + 1",
        "precedence(operator)",
        "subtraction left associative",
    )
    return FamilyDefinition(
        "expression_pipeline",
        "test",
        "Evaluate expression containing nonnegative decimal integer literals, binary + - *, parentheses, "
        "spaces/tabs/newlines. Multiplication binds tighter; binary operators are left associative. "
        "No unary operators or implicit multiplication. Consume the complete input. Empty/malformed input "
        "or unknown characters raise ValueError. Return integer; never use eval or external imports.",
        (
            (
                "tokenizing.py",
                r"""
                def tokenize(text):
                    tokens = []
                    position = 0
                    while position < len(text):
                        char = text[position]
                        if char in ' \t\n':
                            position += 1
                            continue
                        if '0' <= char <= '9':
                            end = position + 1
                            while end < len(text) and '0' <= text[end] <= '9':
                                end += 1
                            tokens.append(text[position:end])
                            position = end
                            continue
                        if char in '+-*()':
                            tokens.append(char)
                        else:
                            raise ValueError('character')
                        position += 1
                    return tokens
            """,
            ),
            (
                "operations.py",
                """
                def precedence(token):
                    return 2 if token == '*' else 1

                def apply(operator, left, right):
                    if operator == '+':
                        return left + right
                    if operator == '-':
                        return left - right
                    return left * right
            """,
            ),
            (
                "parsing.py",
                """
                from operations import precedence, apply

                def atom(tokens, position):
                    if position >= len(tokens):
                        raise ValueError('operand')
                    token = tokens[position]
                    if token == '(':
                        value, end = expression(tokens, position + 1, 0)
                        if end >= len(tokens) or tokens[end] != ')':
                            raise ValueError('closing parenthesis')
                        return value, end + 1
                    if token in ('+', '-', '*', ')'):
                        raise ValueError('operand')
                    return int(token), position + 1

                def expression(tokens, position, minimum):
                    left, position = atom(tokens, position)
                    while position < len(tokens) and tokens[position] in ('+', '-', '*'):
                        operator = tokens[position]
                        if precedence(operator) < minimum:
                            break
                        right, position = expression(tokens, position + 1, precedence(operator) + 1)
                        left = apply(operator, left, right)
                    return left, position

                def parse(tokens):
                    value, end = expression(tokens, 0, 0)
                    if end != len(tokens):
                        raise ValueError('trailing input')
                    return value
            """,
            ),
        ),
        "from tokenizing import tokenize\nfrom parsing import parse",
        "parse(tokenize(payload['expression']))",
        ("expression",),
        ((precedence, associativity), (low_precedence, associativity)),
        (
            Mutation(
                "parsing.py",
                "if end != len(tokens):",
                "if end > len(tokens):",
                "complete token consumption",
            ),
            Mutation(
                "tokenizing.py",
                "raise ValueError('character')",
                "pass",
                "unknown characters rejected",
            ),
        ),
        """
        assert solve({'expression':'2 + 3 * 4'}) == 14, 'contract: precedence'
        assert solve({'expression':'9 - 4 - 2'}) == 3, 'contract: left association'
        assert solve({'expression':'(2+3)*4'}) == 20, 'contract: parentheses'
        for text in ('1 2', '2x+3', '', '-1', '2+', '(3', '1)'):
            try:
                solve({'expression':text})
            except ValueError:
                pass
            else:
                raise AssertionError('contract: invalid/complete expression')
        """,
        """
        assert solve({'expression':'15 - 6 - 3'}) == 6
        assert solve({'expression':'1+2*5+4'}) == 15
        assert solve({'expression':'(7-10)*(2+1)'}) == -9
        assert solve({'expression':'42'}) == 42
        for text in ('4 5', '8?+1', '()', '3**2', '3(2)'):
            try:
                solve({'expression':text})
            except ValueError:
                pass
            else:
                raise AssertionError('invalid independent expression')
        """,
    )
