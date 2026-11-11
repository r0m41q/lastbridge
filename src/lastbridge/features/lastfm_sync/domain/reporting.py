"""Validation and initial delivery state, independent of persistence."""

from datetime import datetime

from lastbridge.shared.errors import CatalogError


def timestamp(value, now):
    try:
        try:
            stamp = int(value) // 1000
        except ValueError:
            # Navic sends an ISO instant instead of Subsonic milliseconds.
            instant = datetime.fromisoformat(value)
            if instant.tzinfo is None:
                raise ValueError from None
            stamp = int(instant.timestamp())
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
