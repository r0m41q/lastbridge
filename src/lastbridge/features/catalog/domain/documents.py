"""Stable metadata identity, image and numeric rules."""

import re
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

SOURCE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
IMAGE_HOSTS = frozenset(
    {"lh3.googleusercontent.com", "yt3.googleusercontent.com", "i.ytimg.com"}
)


def identity(kind, source):
    return (
        kind[0]
        + uuid5(NAMESPACE_URL, f"lastbridge:youtube:{kind}:{source}").hex
    )


def image_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme == "https"
            and parsed.hostname in IMAGE_HOSTS
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        ):
            return value
    except ValueError:
        pass
    return None


def thumbnail(row):
    for item in reversed(row.get("thumbnails") or []):
        if url := image_url(item.get("url")):
            return url
    return None


def number(value):
    try:
        result = int(value)
        return result if 0 <= result <= 2**31 - 1 else None
    except ValueError, TypeError, OverflowError:
        return None
