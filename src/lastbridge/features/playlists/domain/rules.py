from datetime import UTC, datetime, timedelta

from lastbridge.shared.errors import CatalogError

MAX_TRACKS = 1000
MAX_PLAYLISTS = 200


def label(value, field, maximum, *, empty=False):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not empty and not value.strip())
        or any(
            not (
                c in "\t\n\r"
                or 0x20 <= ord(c) <= 0xD7FF
                or 0xE000 <= ord(c) <= 0xFFFD
                or 0x10000 <= ord(c) <= 0x10FFFF
            )
            for c in value
        )
    ):
        raise CatalogError(10, f"Invalid parameter: {field}")
    return value


def touch(row):
    now = datetime.now(UTC)
    if row is not None:
        now = max(
            now,
            datetime.fromisoformat(row["changed"]) + timedelta(milliseconds=1),
        )
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
