"""Normalize public playlist metadata; resolve exact official audio only."""

import re

from lastbridge.features.catalog.domain.documents import image_url
from lastbridge.features.playlists.domain.imports import MAX_ENTRIES, Snapshot
from lastbridge.shared.errors import CatalogError, ProviderError


class PlaylistProvider:
    def __init__(self, fetch, song, match):
        self.fetch, self.song, self.match = fetch, song, match

    async def load(self, source):
        data = await self.fetch({"operation": "playlist", "source": source})
        tracks, title = data.get("tracks"), data.get("title")
        count = data.get("trackCount")
        if (
            not isinstance(title, str)
            or not 0 < len(title) <= 200
            or not isinstance(tracks, list)
            or len(tracks) > MAX_ENTRIES
            or (isinstance(count, int) and count > MAX_ENTRIES)
        ):
            raise CatalogError(
                10, "Playlist unavailable or exceeds 1000 entries"
            )
        if isinstance(count, int) and count > len(tracks):
            raise CatalogError(0, "Incomplete playlist response; retry later")
        entries, matches, lookups = [], {}, 0
        for position, row in enumerate(tracks, 1):
            entry = {
                "position": position,
                "title": "Unavailable track",
                "artist": "",
                "song_id": None,
                "reason": "Unavailable",
            }
            if isinstance(row, dict):
                title = row.get("title")
                artists = row.get("artists")
                if isinstance(title, str) and 0 < len(title) <= 1024:
                    entry["title"] = title
                names = (
                    [
                        a["name"]
                        for a in artists
                        if isinstance(a, dict)
                        and isinstance(a.get("name"), str)
                        and 0 < len(a["name"]) <= 256
                    ]
                    if isinstance(artists, list)
                    else []
                )
                entry["artist"] = ", ".join(names)[:1024]
                source_id = row.get("videoId")
                album = row.get("album")
                song = None
                if (
                    isinstance(source_id, str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{11}", source_id)
                    and entry["title"] != "Unavailable track"
                    and names
                    and row.get("isAvailable") is not False
                ):
                    safe = row | {
                        "artists": [
                            {
                                "name": a["name"],
                                "id": a.get("id")
                                if isinstance(a.get("id"), str)
                                and re.fullmatch(
                                    r"[A-Za-z0-9_-]{1,128}", a["id"]
                                )
                                else None,
                            }
                            for a in artists
                            if isinstance(a, dict) and a.get("name") in names
                        ]
                    }
                    if (
                        isinstance(album, dict)
                        and isinstance(album.get("id"), str)
                        and re.fullmatch(
                            r"MPRE[A-Za-z0-9_-]{1,124}", album["id"]
                        )
                        and isinstance(album.get("name"), str)
                        and 0 < len(album["name"]) <= 1024
                    ):
                        safe["album"] = {
                            "id": album["id"],
                            "name": album["name"],
                        }
                        thumbnails = row.get("thumbnails")
                        safe["thumbnails"] = (
                            [
                                {"url": image_url(t.get("url"))}
                                for t in thumbnails[:8]
                                if isinstance(t, dict)
                                and image_url(t.get("url"))
                            ]
                            if isinstance(thumbnails, list)
                            else []
                        )
                        duration = row.get("duration_seconds")
                        length = row.get("duration")
                        if duration is None and isinstance(length, str):
                            if re.fullmatch(
                                r"\d{1,2}:\d{2}(?::\d{2})?", length
                            ):
                                parts = [
                                    int(part) for part in length.split(":")
                                ]
                                if all(part < 60 for part in parts[1:]):
                                    duration = 0
                                    for part in parts:
                                        duration = duration * 60 + part
                        safe["duration_seconds"] = duration
                        song = self.song(safe)
                    if song is None:
                        key = (names[0], entry["title"])
                        if key not in matches and lookups < 50:
                            lookups += 1
                            try:
                                matches[key] = await self.match(*key)
                            except ProviderError:
                                matches[key] = None
                            except CatalogError as error:
                                if error.code != 70:
                                    raise
                                matches[key] = None
                        song = matches.get(key)
                        entry["reason"] = (
                            "No exact audio match"
                            if key in matches
                            else "Fallback search limit reached (50)"
                        )
                if song:
                    entry["song_id"], entry["reason"] = song["id"], None
            entries.append(entry)
        return Snapshot(data["title"], entries)
