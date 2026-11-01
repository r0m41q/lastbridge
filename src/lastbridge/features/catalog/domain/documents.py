"""Stable metadata identity and document rules.

client_document preserves the stored legacy representation until R3 moves the
remaining client representation boundary. No persisted document format changes.
"""

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


def client_document(kind, document):
    """Make persisted pre-rendition metadata safe for client song parsers."""
    document = dict(document)
    if kind == "song":
        # Feishin unconditionally calls contentType.startsWith(). Unknown media
        # needs a string without inventing an audio format before resolution.
        document.setdefault("contentType", "application/octet-stream")
    child = {"album": "song", "artist": "album"}.get(kind)
    if child and child in document:
        document[child] = [client_document(child, d) for d in document[child]]
    return document
