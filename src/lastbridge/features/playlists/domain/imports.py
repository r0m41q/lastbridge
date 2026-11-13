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


def normalized_entries(entries):
    return [
        entry
        | {
            "source_key": entry.get("source_key")
            or f"unavailable:{entry['position']}",
        }
        for entry in entries
    ]


def reviewed_entries(entries, decisions):
    return [
        entry
        | {
            "song_id": decision["song_id"],
            "reason": None,
            **({"skipped": True} if decision.get("skipped") else {}),
            "reviewed": True,
        }
        if (decision := decisions.get(entry.get("source_key")))
        else entry
        for entry in entries
    ]


def lastfm_source(source):
    return source.startswith("lastfm:")


def playlist_source(value, lastfm_name=None):
    if isinstance(value, str) and len(value) <= 2048:
        value = value.strip()
        playlist_id = value if re.fullmatch(r"[0-9]{1,20}", value) else None
        try:
            url = urlsplit(value)
            if (
                url.scheme == "https"
                and url.hostname in {"www.last.fm", "last.fm"}
                and url.port in (None, 443)
                and url.username is None
                and url.password is None
            ):
                match = re.fullmatch(
                    r"/(?:[a-z]{2}/)?user/[^/]+/playlists/([0-9]{1,20})/?",
                    url.path,
                )
                playlist_id = match[1] if match else None
        except ValueError:
            pass
        if playlist_id is not None:
            if not isinstance(lastfm_name, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,64}", lastfm_name
            ):
                raise CatalogError(10, "Link Last.fm before importing")
            return f"lastfm:{lastfm_name}:{int(playlist_id)}"
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
            10, "Enter a YouTube or Last.fm playlist URL, or Last.fm ID"
        ) from None
