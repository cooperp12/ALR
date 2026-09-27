"""Bound control prose without treating clipped observations as complete evidence."""
def short(value, limit=360):
    return ' '.join(str(value or '').split())[:limit]


def compact_control(value, depth=0):
    if depth > 4:
        return '[nested state omitted]'
    if isinstance(value, str):
        return short(value)
    if isinstance(value, dict):
        return {k: compact_control(v, depth+1) for k, v in list(value.items())[:20]
                if k not in {'supervisor_feedback', 'supervisor_history', 'machine_result'}}
    if isinstance(value, (list, tuple)):
        return [compact_control(v, depth+1) for v in value[-6:]]
    return value
