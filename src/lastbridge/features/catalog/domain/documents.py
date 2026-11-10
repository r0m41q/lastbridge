"""Stable metadata identity, image and numeric rules."""

import re
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

SOURCE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
IMAGE_HOSTS = frozenset(
    {
        "lh3.googleusercontent.com",
        "yt3.googleusercontent.com",
        "yt3.ggpht.com",
        "i.ytimg.com",
    }
)
GOOGLE_SIZE = re.compile(
    r"^(https://(?:lh3|yt3)\.googleusercontent\.com/[^?]*?)"
    r"=w\d+-h\d+[^?]*(\?.*)?$"
)
GGPHT_SIZE = re.compile(
    r"^(https://yt3\.ggpht\.com/[^?=]+)"
    r"=(?:s\d+|w\d+-h\d+)[^?]*(\?.*)?$"
)


def artwork_url(value):
    """Ask Google's image server for 1080px artwork, as Metrolist does."""
    url = image_url(value)
    if url is None:
        return None
    host = urlsplit(url).hostname
    pattern = (
        GOOGLE_SIZE
        if host in ("lh3.googleusercontent.com", "yt3.googleusercontent.com")
        else GGPHT_SIZE
        if host == "yt3.ggpht.com"
        else None
    )
    match = pattern.fullmatch(url) if pattern is not None else None
    if match is None:
        return url
    return f"{match[1]}=w1080-h1080-p-l90-rj{match[2] or ''}"


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
    candidates = []
    for position, item in enumerate(row.get("thumbnails") or []):
        if not isinstance(item, dict):
            continue
        if url := image_url(item.get("url")):
            width, height = (
                number(item.get("width")),
                number(item.get("height")),
            )
            candidates.append(((width or 0) * (height or 0), position, url))
    best = max(candidates, default=None)
    return best[-1] if best else None


def number(value):
    try:
        result = int(value)
        return result if 0 <= result <= 2**31 - 1 else None
    except ValueError, TypeError, OverflowError:
        return None
