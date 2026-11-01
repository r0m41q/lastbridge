"""Validation and initial delivery state, independent of persistence."""

from lastbridge.shared.errors import CatalogError


def timestamp(value, now):
    try:
        stamp = int(value) // 1000
        if stamp <= 0 or stamp > now + 300:
            raise ValueError
    except ValueError:
        raise CatalogError(10, "Invalid parameter: time") from None
    return stamp


def initial_state(duration, artist):
    if duration <= 30:
        return "ignored", "Track is not longer than 30 seconds."
    if not artist:
        return "failed", "Track artist is missing."
    return "pending", None
