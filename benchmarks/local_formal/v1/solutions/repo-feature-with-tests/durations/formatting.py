def format_duration(seconds: int) -> str:
    if seconds < 0: raise ValueError('negative duration')
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return (f'{hours}h' if hours else '') + (f'{minutes}m' if minutes else '') + (f'{secs}s' if secs or seconds == 0 else '')
