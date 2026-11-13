"""Validate public playlist metadata and resolve resumable batches."""

import asyncio
import json
import re
import time

from lastbridge.features.catalog.domain.documents import image_url
from lastbridge.features.playlists.adapters.youtube.matching import (
    PlaylistResolver,
)
from lastbridge.features.playlists.domain.imports import (
    MAX_ENTRIES,
    Snapshot,
    reviewed_entries,
)
from lastbridge.features.playlists.domain.matching import VERSION, identity
from lastbridge.shared.errors import CatalogError, ProviderError

BATCH_SIZE = 10
LOOKUP_TIMEOUT = 30


class PlaylistProvider:
    def __init__(self, fetch, song, match=None):
        self.fetch, self.song, self.match = fetch, song, match
        self.resolver = (
            PlaylistResolver(fetch, song) if match is None else None
        )

    async def prepare(self, source, previous=None):
        async with asyncio.timeout(125):
            data = await self.fetch(
                {"operation": "playlist", "source": source}
            )
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
        matches = (previous or {}).get("matches", {})
        matches = {
            key: value
            for key, value in matches.items()
            if value["time"] > time.time() - (86400 if value["song"] else 300)
        }
        # Only public fields are persisted, never provider/session tokens.
        fields = {
            "videoId",
            "title",
            "artists",
            "album",
            "videoType",
            "duration_seconds",
            "duration",
            "thumbnails",
            "isAvailable",
            "isExplicit",
        }
        result = {
            "title": title,
            "tracks": [
                self.public_row(row, fields) if isinstance(row, dict) else None
                for row in tracks
            ],
            "entries": [],
            "originals": (previous or {}).get("originals", {}),
            "matches": matches,
            "resolver_version": VERSION,
            "decisions": {
                key: value
                for key, value in (previous or {}).get("decisions", {}).items()
                if value.get("skipped")
                or key
                in {identity(row) for row in tracks if isinstance(row, dict)}
            },
        }

        snapshot = (previous or {}).get("snapshot")
        if (
            snapshot
            and snapshot.get("resolver_version") == VERSION
            and snapshot.get("tracks") == result["tracks"]
        ):
            result["entries"] = snapshot["entries"]
        return result

    @staticmethod
    def public_row(row, fields):
        safe = {key: value for key, value in row.items() if key in fields}
        if isinstance(row.get("artists"), list):
            safe["artists"] = [
                {key: artist.get(key) for key in ("name", "id")}
                for artist in row["artists"][:100]
                if isinstance(artist, dict)
            ]
        if isinstance(row.get("album"), dict):
            safe["album"] = {
                key: row["album"].get(key) for key in ("name", "id")
            }
        if isinstance(row.get("thumbnails"), list):
            safe["thumbnails"] = [
                {"url": image_url(thumbnail.get("url"))}
                for thumbnail in row["thumbnails"][:8]
                if isinstance(thumbnail, dict)
                and image_url(thumbnail.get("url"))
            ]
        return safe

    def entry(self, row, position):
        song, key = None, None
        entry = {
            "position": position,
            "source_key": identity(row)
            if isinstance(row, dict)
            else f"unavailable:{position}",
            "title": "Unavailable track",
            "artist": "",
            "song_id": None,
            "reason": "Unavailable",
            "duration": row.get("duration_seconds")
            if isinstance(row, dict)
            else None,
            "explicit": row.get("isExplicit")
            if isinstance(row, dict)
            else None,
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
                key = (names[0], entry["title"])
                safe = row | {
                    "artists": [
                        {
                            "name": a["name"],
                            "id": a.get("id")
                            if isinstance(a.get("id"), str)
                            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", a["id"])
                            else None,
                        }
                        for a in artists
                        if isinstance(a, dict) and a.get("name") in names
                    ]
                }
                if (
                    isinstance(album, dict)
                    and isinstance(album.get("id"), str)
                    and re.fullmatch(r"MPRE[A-Za-z0-9_-]{1,124}", album["id"])
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
                            if isinstance(t, dict) and image_url(t.get("url"))
                        ]
                        if isinstance(thumbnails, list)
                        else []
                    )
                    duration = row.get("duration_seconds")
                    length = row.get("duration")
                    if duration is None and isinstance(length, str):
                        if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", length):
                            parts = [int(part) for part in length.split(":")]
                            if all(part < 60 for part in parts[1:]):
                                duration = 0
                                for part in parts:
                                    duration = duration * 60 + part
                    safe["duration_seconds"] = duration
                    song = self.song(safe)
        return entry, song, key

    async def batch(self, progress):
        if (
            self.resolver
            and self.match is None
            and progress.get("resolver_version") != VERSION
        ):
            progress = progress | {
                "entries": [],
                "matches": {},
                "resolver_version": VERSION,
            }
        # A failed or interrupted batch never mutates the saved checkpoint.
        result = progress | {
            "entries": list(progress["entries"]),
            "matches": dict(progress["matches"]),
        }
        matches, pending = result["matches"], {}
        slots = asyncio.Semaphore(2)

        async def lookup(key):
            async with slots, asyncio.timeout(LOOKUP_TIMEOUT):
                try:
                    song = await self.match(*key)
                except ProviderError:
                    song = None
                except CatalogError as error:
                    if error.code != 70:
                        raise
                    song = None
                return {"id": song["id"]} if song else None

        async def resolve(row, position):
            entry, song, key = self.entry(row, position)
            if self.resolver and self.match is None and key is not None:
                source_key = identity(row)
                entry["source_key"] = source_key
                decision = progress.get("decisions", {}).get(source_key)
                if decision:
                    original = progress.get("originals", {}).get(source_key)
                    if original:
                        entry = original | {"position": position}
                    elif not decision.get("skipped"):
                        entry["candidates"] = [decision]
                    return reviewed_entries([entry], {source_key: decision})[0]
                if song is None:
                    cache_key = "v2:" + source_key
                    cached = matches.get(cache_key)
                    if cached and cached["time"] <= time.time() - (
                        86400 if cached["song"] else 300
                    ):
                        cached = None
                    if cached is None:

                        async def search():
                            async with slots, asyncio.timeout(LOOKUP_TIMEOUT):
                                self.resolver.fetch = self.fetch
                                return await self.resolver.resolve(row)

                        if cache_key not in pending:
                            pending[cache_key] = asyncio.create_task(search())
                        resolved = await asyncio.shield(pending[cache_key])
                        cached = {"time": time.time(), **resolved}
                        matches[cache_key] = cached
                    song = cached["song"]
                    entry.update(
                        outcome=cached["outcome"],
                        candidates=cached["candidates"],
                        reason=cached["reason"],
                    )
                if song:
                    entry.update(song_id=song["id"], reason=None)
                return entry
            if song is None and key is not None:
                cache_key = json.dumps(key)
                cached = matches.get(cache_key)
                if cached and cached["time"] <= time.time() - (
                    86400 if cached["song"] else 300
                ):
                    cached = None
                if cached is None:
                    if cache_key not in pending:
                        pending[cache_key] = asyncio.create_task(lookup(key))
                    song = await asyncio.shield(pending[cache_key])
                    matches[cache_key] = {"time": time.time(), "song": song}
                else:
                    song = cached["song"]
                entry["reason"] = "No exact audio match"
            if song:
                entry["song_id"], entry["reason"] = song["id"], None
            return entry

        start = len(result["entries"])
        tasks = [
            asyncio.create_task(resolve(row, i + 1))
            for i, row in enumerate(
                result["tracks"][start : start + BATCH_SIZE], start
            )
        ]
        try:
            result["entries"].extend(await asyncio.gather(*tasks))
        finally:
            for task in [*tasks, *pending.values()]:
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(
                *tasks, *pending.values(), return_exceptions=True
            )
        # Retain only a bounded cache even when a chart changes every day.
        result["matches"] = dict(
            sorted(
                matches.items(), key=lambda item: item[1]["time"], reverse=True
            )[:2000]
        )
        return result

    async def load(self, source):
        """Complete snapshot helper; workers checkpoint each batch."""
        progress = await self.prepare(source)
        while len(progress["entries"]) < len(progress["tracks"]):
            progress = await self.batch(progress)
        return Snapshot(progress["title"], progress["entries"])
