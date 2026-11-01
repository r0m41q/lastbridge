from datetime import UTC, datetime


def date(stamp):
    return (
        datetime.fromtimestamp(stamp, UTC).isoformat().replace("+00:00", "Z")
    )
