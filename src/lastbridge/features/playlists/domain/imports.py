"""Public playlist identity and normalized import results."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from lastbridge.shared.errors import CatalogError

DAY = 86400
MAX_ENTRIES = 1000


@dataclass(frozen=True)
class Snapshot:
    title: str
    entries: list[dict]


def playlist_source(value):
    try:
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError
        url = urlsplit(value.strip())
        query = parse_qs(url.query)
        (source,) = query.get("list", [])
        if (
            url.scheme != "https"
            or url.hostname
            not in {"music.youtube.com", "www.youtube.com", "youtube.com"}
            or url.port not in (None, 443)
            or url.username is not None
            or url.password is not None
            or url.path != "/playlist"
            or url.fragment
            or not re.fullmatch(
                r"(?:PL|OLAK5uy_|RDCLAK)[A-Za-z0-9_-]{8,120}", source
            )
        ):
            raise ValueError
        return source
    except ValueError, AttributeError, TypeError:
        raise CatalogError(
            10, "Enter a public HTTPS YouTube playlist URL"
        ) from None
