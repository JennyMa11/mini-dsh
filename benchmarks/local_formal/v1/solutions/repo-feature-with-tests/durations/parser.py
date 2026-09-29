import re

def parse_duration(text: str) -> int:
    if not text: raise ValueError('empty duration')
    total = 0
    position = 0
    previous = 4
    ranks = {'h': 3, 'm': 2, 's': 1}
    for match in re.finditer(r'(\d+)([hms])', text):
        if match.start() != position or ranks[match[2]] >= previous:
            raise ValueError('invalid duration')
        total += int(match[1]) * {'h': 3600, 'm': 60, 's': 1}[match[2]]
        position = match.end()
        previous = ranks[match[2]]
    if position != len(text): raise ValueError('invalid duration')
    return total
