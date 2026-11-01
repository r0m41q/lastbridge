import asyncio
import hashlib
import json
import time

from lastbridge.features.lyrics.application.ports import (
    CatalogAccess,
    LyricsProvider,
    LyricsStore,
)
from lastbridge.features.lyrics.domain.errors import (
    CoolingDown,
    LyricsUnavailable,
)
from lastbridge.features.lyrics.domain.lyrics import text
from lastbridge.shared.errors import StorageError

DEADLINE = 8


class Lyrics:
    def __init__(
        self,
        repository: LyricsStore,
        catalog: CatalogAccess,
        provider: LyricsProvider,
    ):
        self.repository, self.catalog, self.provider = (
            repository,
            catalog,
            provider,
        )
        self.tasks = {}
        self.closed = False

    def cached(self, song_id, fingerprint):
        return self.repository.cached(song_id, fingerprint)

    def save(self, song_id, fingerprint, document, ttl, updated=None):
        self.repository.save(song_id, fingerprint, document, ttl, updated)

    async def fetch(self, song_id, fingerprint, song, previous):
        stale = []
        if previous and previous["updated"] >= time.time() - 30 * 86400:
            stale = json.loads(previous["document"])
        try:
            async with asyncio.timeout(DEADLINE):
                result = await self.provider.get(song)
            # The fingerprint invalidates this entry if song metadata changes.
            self.save(
                song_id, fingerprint, result, 7 * 86400 if result else 86400
            )
            return result
        except CoolingDown:
            return stale
        except LyricsUnavailable, TimeoutError, ValueError, StorageError:
            # Briefly cache transient failures to avoid repeated requests.
            try:
                self.save(
                    song_id,
                    fingerprint,
                    stale,
                    60,
                    previous["updated"] if previous and stale else None,
                )
            except StorageError:
                pass
            return stale

    async def get(self, song_id, *, fetch=True):
        song = self.catalog.read(song_id, "song")["document"]
        if not fetch or self.closed:
            return []
        if any(
            not text(song.get(key)) or not 0 < len(song[key]) <= 1024
            for key in ("artist", "title", "album")
        ) or not (
            type(song.get("duration")) is int and 1 <= song["duration"] <= 3600
        ):
            return []
        signature = {
            k: song[k] for k in ("artist", "title", "album", "duration")
        }
        fingerprint = hashlib.sha256(
            json.dumps(signature, sort_keys=True).encode()
        ).hexdigest()
        try:
            cached = self.cached(song_id, fingerprint)
            if cached and cached["expires"] > time.time():
                return json.loads(cached["document"])
        except StorageError, ValueError:
            return []
        key = (song_id, fingerprint)
        if key not in self.tasks:
            if len(self.tasks) >= 4:
                return []
            task = asyncio.create_task(
                self.fetch(song_id, fingerprint, signature, cached)
            )
            self.tasks[key] = task
            task.add_done_callback(lambda _: self.tasks.pop(key, None))
        return await asyncio.shield(self.tasks[key])

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
