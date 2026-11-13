import asyncio
import json
import math
import re

import httpx

from lastbridge.features.playlists.adapters.youtube.imports import (
    PlaylistProvider,
)
from lastbridge.features.playlists.domain.imports import MAX_ENTRIES
from lastbridge.shared.errors import CatalogError

MAX_RESPONSE = 2 * 1024 * 1024
UNAVAILABLE = "Last.fm playlist unavailable for the linked user"


class LastfmPlaylistProvider(PlaylistProvider):
    def __init__(self, client, fetch, song):
        super().__init__(fetch, song)
        self.client = client

    async def prepare(self, source, previous=None):
        match = re.fullmatch(
            r"lastfm:([A-Za-z0-9_-]{1,64}):([0-9]{1,20})", source
        )
        if not match:
            raise CatalogError(10, "Invalid Last.fm playlist source")
        username, playlist_id = match.groups()
        url = (
            "https://www.last.fm/player/station/user/"
            f"{username}/playlists/{playlist_id}"
        )
        tracks = []
        async with asyncio.timeout(125):
            for page in range(1, MAX_ENTRIES + 2):
                rows = await self.page(url, page)
                if not rows:
                    if page == 1:
                        # An empty response cannot establish a readable source.
                        raise CatalogError(70, UNAVAILABLE + " or empty")
                    break
                if len(rows) + len(tracks) > MAX_ENTRIES:
                    raise CatalogError(10, "Playlist exceeds 1000 entries")
                tracks.extend(self.track(row) for row in rows)
            else:
                raise CatalogError(0, "Incomplete Last.fm playlist response")
        return self.prepare_data(
            {"title": f"Last.fm playlist {playlist_id}", "tracks": tracks},
            previous,
        )

    async def page(self, url, page):
        try:
            async with self.client.stream(
                "GET",
                url,
                params={"page": page},
                headers={"Accept": "application/json"},
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise CatalogError(
                        0 if response.status_code >= 500 else 70, UNAVAILABLE
                    )
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_RESPONSE:
                        raise CatalogError(0, "Last.fm response too large")
            data = json.loads(payload)
        except httpx.HTTPError:
            raise CatalogError(0, "Last.fm playlist request failed") from None
        except ValueError, UnicodeError:
            raise CatalogError(
                0, "Invalid Last.fm playlist response"
            ) from None
        rows = data.get("playlist") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise CatalogError(0, "Invalid Last.fm playlist response")
        return rows

    @staticmethod
    def track(row):
        if not isinstance(row, dict):
            raise CatalogError(0, "Invalid Last.fm track metadata")
        title, artists = row.get("name"), row.get("artists")
        if (
            not isinstance(title, str)
            or not 0 < len(title.strip()) <= 1024
            or not isinstance(artists, list)
            or not 0 < len(artists) <= 100
            or any(
                not isinstance(a, dict)
                or not isinstance(a.get("name"), str)
                or not 0 < len(a["name"].strip()) <= 256
                for a in artists
            )
        ):
            raise CatalogError(0, "Invalid Last.fm track metadata")
        duration = row.get("duration")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or not 0 < duration <= 86400
        ):
            duration = None
        return {
            "source_kind": "lastfm",
            "title": title.strip(),
            "artists": [{"name": a["name"].strip()} for a in artists],
            "duration_seconds": int(duration) if duration else None,
        }

    @staticmethod
    def valid_source(row):
        # Match Last.fm metadata to audio; ignore its video suggestions.
        return row.get("source_kind") == "lastfm"
