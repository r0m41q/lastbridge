import asyncio
import json
import logging
import time
from collections import OrderedDict
from uuid import NAMESPACE_URL, uuid5

from lastbridge.features.radio.application.fallback import SimilarTracks
from lastbridge.features.radio.application.ports import (
    CatalogAccess,
    LibraryAccess,
    RadioStore,
    TasteProvider,
)
from lastbridge.shared.dates import date
from lastbridge.shared.errors import CatalogError, StorageError
from lastbridge.shared.recordings import recording_key

logger = logging.getLogger(__name__)
DEADLINE = 20
TTL = 900
PRIMARY_DEADLINE = 6


class Radio:
    def __init__(
        self,
        repository: RadioStore,
        catalog: CatalogAccess,
        library: LibraryAccess,
        remote: TasteProvider,
    ):
        self.repository, self.catalog, self.library, self.remote = (
            repository,
            catalog,
            library,
            remote,
        )
        self.similar = SimilarTracks(catalog, remote)
        self.cache = OrderedDict()
        self.tasks = {}
        self.warmed = OrderedDict()
        self.generations = {}
        self.slots = asyncio.Semaphore(2)
        self.closed = False

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    async def fetch(self, source, fallback=False):
        key = ("lastfm", source) if fallback else source
        try:
            async with asyncio.timeout(DEADLINE):
                await asyncio.wait_for(self.slots.acquire(), timeout=1)
                try:
                    if fallback:
                        songs = await self.fallback(source)
                    else:
                        async with asyncio.timeout(PRIMARY_DEADLINE):
                            songs = await self.catalog.radio(source)
                finally:
                    self.slots.release()
            self.cache[key] = (time.monotonic(), songs)
            self.cache.move_to_end(key)
            while len(self.cache) > 32:
                self.cache.popitem(last=False)
            return songs
        except TimeoutError:
            raise CatalogError(
                0, "Radio metadata timed out; retry later"
            ) from None

    def task(self, source, fallback=False):
        key = ("lastfm", source) if fallback else source
        if self.closed:
            raise CatalogError(0, "Radio is stopping")
        if key in self.tasks:
            return self.tasks[key]
        if len(self.tasks) >= 4:
            raise CatalogError(0, "Radio metadata busy; retry later")
        task = self.tasks[key] = asyncio.create_task(
            self.fetch(source, fallback)
        )

        def finished(done):
            if self.tasks.get(key) is done:
                del self.tasks[key]
            # A disconnected caller or warm-only task still consumes errors.
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return task

    def warm(self, user_id, song_id):
        """Metadata only; never infer a play, favorite or radio selection."""
        now = time.monotonic()
        if self.closed or self.warmed.get(user_id, 0) > now - 60:
            return
        try:
            source = self.library.recording(user_id, song_id, "song")["source"]
            if source in self.cache and self.cache[source][0] > now - TTL:
                return
            self.warmed[user_id] = now
            self.warmed.move_to_end(user_id)
            while len(self.warmed) > 128:
                self.warmed.popitem(last=False)
            self.task(source)
        except CatalogError, StorageError, OSError:
            # A best-effort metadata warm must not prevent playback.
            logger.debug("Radio metadata warm skipped")

    def filter(self, user, seed, songs):
        recent_ids, removed, ids = self.repository.exclusions(user)
        ids.add(seed["id"])
        recent = set()
        for song_id in recent_ids:
            document = self.catalog.read(song_id, "song")["document"]
            recent.add(
                recording_key(document.get("artist", ""), document["title"])
            )
        seen = (
            recent
            | removed
            | {recording_key(seed.get("artist", ""), seed["title"])}
        )
        result = []
        for song in songs:
            key = recording_key(song.get("artist", ""), song["title"])
            if song["id"] in ids or key in seen:
                continue
            seen.add(key)
            ids.add(song["id"])
            result.append(song)
        return result

    async def seed(self, item_id):
        item = self.catalog.read(item_id)
        if item["kind"] == "song":
            return item["document"]
        if item["kind"] == "artist":
            song_id = self.repository.artist_song(item_id)
            if song_id:
                return self.catalog.read(song_id, "song")["document"]
            artist = await self.catalog.detail(item_id, "artist")
            albums = artist.get("album", [])
            if not albums:
                raise CatalogError(70, "No available radio seed for artist")
            item_id = albums[0]["id"]
        album = await self.catalog.detail(item_id, "album")
        songs = album.get("song", [])
        if not songs:
            raise CatalogError(70, "No available radio seed for album")
        return songs[0]

    def snapshot(self, user):
        return self.repository.snapshot(user)

    def publish(self, user, seed, songs, generation):
        if self.generations.get(user["id"]) is generation:
            self.repository.publish(user, seed, songs)

    async def fallback(self, source):
        return await self.similar.fallback(source)

    async def songs(self, user, item_id, count):
        self.catalog.read(item_id)  # Validate even a zero-count request.
        if count == 0:
            return []
        # Bound concurrent callers as well as background fetches. Generation
        # tokens prevent a slow older seed replacing a newer explicit request.
        if len(self.generations) >= 128 and user["id"] not in self.generations:
            raise CatalogError(0, "Radio busy; retry later")
        generation = object()
        self.generations[user["id"]] = generation
        seed = None
        try:
            async with asyncio.timeout(DEADLINE):
                seed = await self.seed(item_id)
                recording = self.library.recording(
                    user["id"], seed["id"], "song"
                )
                source = recording["source"]
                seed = recording["document"]
                cached = self.cache.get(source)
                if cached and cached[0] > time.monotonic() - TTL:
                    self.cache.move_to_end(source)
                    candidates = cached[1]
                else:
                    try:
                        candidates = await asyncio.shield(self.task(source))
                    except CatalogError:
                        candidates = []
                result = self.filter(user, seed, candidates)
                # Exhausted pool: allow a fresh YouTube radio at most once a
                # minute, rather than returning the same recently played songs.
                if not result and cached and cached[0] < time.monotonic() - 60:
                    try:
                        candidates = await asyncio.shield(self.task(source))
                    except CatalogError:
                        candidates = []
                    result = self.filter(user, seed, candidates)
                if not result:
                    fallback = self.cache.get(("lastfm", source))
                    age = TTL if fallback and fallback[1] else 60
                    if fallback and fallback[0] > time.monotonic() - age:
                        self.cache.move_to_end(("lastfm", source))
                        candidates = fallback[1]
                    else:
                        candidates = await asyncio.shield(
                            self.task(source, fallback=True)
                        )
                    result = self.filter(user, seed, candidates)
                if not result:
                    raise CatalogError(
                        70, "No unplayed radio tracks available"
                    )
                self.publish(user, seed, candidates, generation)
                return self.library.decorate(user["id"], result[:count])
        except (CatalogError, TimeoutError, StorageError, OSError) as error:
            # Fixed categories only, without IDs, provider URLs or user data.
            if isinstance(error, CatalogError):
                reason = {
                    "Metadata provider busy; retry shortly": "metadata busy",
                    "Radio metadata busy; retry later": "radio capacity busy",
                    "Radio metadata timed out; retry later": "timed out",
                    "No unplayed radio tracks available": "no eligible tracks",
                }.get(error.message, "metadata provider failed")
            else:
                reason = (
                    "deadline exceeded"
                    if isinstance(error, TimeoutError)
                    else "storage unavailable"
                )
            logger.warning("Radio could not complete: %s", reason)
            # Same requested seed only: never masquerade a different radio
            # or another user's snapshot as this request's continuation.
            row = self.snapshot(user)
            if row:
                stored = self.catalog.read(item_id)
                if row["seed_id"] == item_id or (
                    stored["kind"] != "song"
                    and seed is not None
                    and row["seed_id"] == seed["id"]
                ):
                    seed = self.catalog.read(row["seed_id"], "song")[
                        "document"
                    ]
                    songs = [
                        self.catalog.read(s, "song")["document"]
                        for s in json.loads(row["songs"])
                    ]
                    result = self.filter(user, seed, songs)
                    if result:
                        return self.library.decorate(
                            user["id"], result[:count]
                        )
            raise CatalogError(
                0, "Radio unavailable; try a personal mix or retry later"
            ) from None
        finally:
            if self.generations.get(user["id"]) is generation:
                del self.generations[user["id"]]

    def playlist(self, user, detail=False):
        row = self.snapshot(user)
        if row is None:
            return None
        seed = self.catalog.read(row["seed_id"], "song")["document"]
        songs = [
            self.catalog.read(s, "song")["document"]
            for s in json.loads(row["songs"])
        ]
        songs = self.filter(user, seed, songs)
        result = {
            "id": self.playlist_id(user["id"]),
            "name": "Radio · " + seed["title"],
            "owner": user["username"],
            "public": False,
            "readonly": True,
            "created": date(row["created"]),
            "changed": date(row["updated"]),
            "songCount": len(songs),
            "duration": sum(s.get("duration", 0) for s in songs),
            "comment": "Latest radio selection. Server-managed; read only.",
        }
        if seed.get("coverArt"):
            result["coverArt"] = seed["coverArt"]
        if detail:
            result["entry"] = self.library.decorate(user["id"], songs)
        return result

    @staticmethod
    def playlist_id(user_id):
        return "p" + uuid5(NAMESPACE_URL, f"lastbridge:radio:{user_id}").hex
