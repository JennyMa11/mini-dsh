def labels(values):
    return list(dict.fromkeys(value.strip().casefold() for value in values if value.strip()))
