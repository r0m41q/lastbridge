"""Public artist enrichment with bounded work and shared metadata caches."""

import asyncio
import time
from collections import OrderedDict
from weakref import WeakValueDictionary

from lastbridge.features.catalog.application.ports import ArtistMetadataPort
from lastbridge.features.catalog.domain.documents import artwork_url
from lastbridge.shared.errors import CatalogError
from lastbridge.shared.recordings import normalize_title

RANKING_TIMEOUT = 25
RESOLUTION_TIMEOUT = 18


def remember(cache, key, value, ttl, limit):
    cache[key] = (time.monotonic() + ttl, value)
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


class ArtistExtras:
    def __init__(
        self,
        provider,
        documents,
        metadata: ArtistMetadataPort | None,
    ):
        self.provider = provider
        self.documents = documents
        self.metadata = metadata
        self.tops = OrderedDict()
        self.infos = OrderedDict()
        self.matches = OrderedDict()
        self.retries = OrderedDict()
        self.locks = WeakValueDictionary()
        self.slots = asyncio.Semaphore(2)

    async def resolve(self, artist, title):
        key = (artist["source"], normalize_title(title))
        cached = self.matches.get(key)
        if cached and cached[0] > time.monotonic():
            self.matches.move_to_end(key)
            return cached[1]
        async with self.slots:
            rows = await self.provider.fetch(
                {
                    "operation": "search",
                    "query": f"{artist['document']['name']} {title}",
                    "kinds": ["songs"],
                }
            )
            song = self.documents.ranked_song(artist, title, rows)
        remember(self.matches, key, song, 86400 if song else 300, 1024)
        return song

    async def ranked(self, artist):
        rows = await self.metadata.top_tracks(artist["document"]["name"])
        titles, seen = [], set()
        for _, title in rows[:50]:
            key = normalize_title(title)
            if key and key not in seen:
                seen.add(key)
                titles.append(title)
        tasks = [asyncio.create_task(self.resolve(artist, t)) for t in titles]
        if not tasks:
            return [], False, False
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=RESOLUTION_TIMEOUT
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Await cleanup so no workers outlive the request or shared client.
            results = await asyncio.gather(*tasks, return_exceptions=True)
        songs, seen = [], set()
        failed = bool(pending)
        missing = False
        for task, result in zip(tasks, results, strict=True):
            if task not in done or isinstance(
                result, (CatalogError, asyncio.CancelledError)
            ):
                failed = True
            elif isinstance(result, BaseException):
                raise result
            elif result and result["id"] not in seen:
                songs.append(result)
                seen.add(result["id"])
            elif result is None:
                missing = True
        if not songs and failed:
            raise CatalogError()
        return songs, failed or missing, failed

    async def top_songs(self, artist, *, fetch=True):
        source = artist["source"]
        cached = self.tops.get(source)
        if cached and cached[0] > time.monotonic():
            self.tops.move_to_end(source)
            return cached[1]
        if not fetch:
            return []
        if not self.metadata or not self.metadata.configured:
            return []
        lock = self.locks.setdefault(("tops", source), asyncio.Lock())
        async with lock:
            cached = self.tops.get(source)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            retry = self.retries.get(source)
            if retry and retry[0] > time.monotonic():
                if cached and cached[0] > time.monotonic() - 86400:
                    return cached[1]
                raise CatalogError()
            try:
                async with asyncio.timeout(RANKING_TIMEOUT):
                    songs, partial, failed = await self.ranked(artist)
                if failed and cached and cached[0] > time.monotonic() - 86400:
                    remember(self.retries, source, None, 300, 128)
                    return cached[1]
                remember(self.tops, source, songs, 300 if partial else 900, 32)
                self.retries.pop(source, None)
                return songs
            except CatalogError, TimeoutError:
                remember(self.retries, source, None, 300, 128)
                if cached and cached[0] > time.monotonic() - 86400:
                    return cached[1]
                raise CatalogError() from None

    async def info(self, artist, *, fetch=True):
        source = artist["source"]
        lock = self.locks.setdefault(("info", source), asyncio.Lock())
        async with lock:
            cached = self.infos.get(source)
            info = cached[1] if cached else {}
            if (
                fetch
                and self.metadata
                and self.metadata.configured
                and (not cached or cached[0] <= time.monotonic())
            ):
                try:
                    async with asyncio.timeout(8):
                        info = await self.metadata.info(
                            artist["document"]["name"]
                        )
                    remember(self.infos, source, info, 86400, 128)
                except CatalogError, TimeoutError:
                    # Artist info is optional. Keep a previous biography or
                    # image-only result and suppress repeated outage requests.
                    remember(self.infos, source, info, 300, 128)
            result = {k: v for k, v in info.items() if k != "similar"}
            if image := artwork_url(artist.get("image")):
                result |= dict.fromkeys(
                    ("smallImageUrl", "mediumImageUrl", "largeImageUrl"), image
                )
            return result, info.get("similar", [])
