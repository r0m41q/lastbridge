import asyncio
import json
import logging
import sqlite3
import time
from collections import OrderedDict
from uuid import NAMESPACE_URL, uuid5

from lastbridge.catalog import CatalogError
from lastbridge.mixes import date
from lastbridge.recordings import recording_key

logger = logging.getLogger(__name__)
DEADLINE = 20
TTL = 900


class Radio:
    """Bounded public metadata pools; private filtering and last radio list."""

    def __init__(self, accounts, catalog, library):
        self.accounts, self.catalog, self.library = accounts, catalog, library
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

    async def fetch(self, source):
        try:
            async with asyncio.timeout(DEADLINE):
                await asyncio.wait_for(self.slots.acquire(), timeout=1)
                try:
                    songs = await self.catalog.radio(source)
                finally:
                    self.slots.release()
            self.cache[source] = (time.monotonic(), songs)
            self.cache.move_to_end(source)
            while len(self.cache) > 32:
                self.cache.popitem(last=False)
            return songs
        except TimeoutError:
            raise CatalogError(
                0, "Radio metadata timed out; retry later"
            ) from None

    def task(self, source):
        if self.closed:
            raise CatalogError(0, "Radio is stopping")
        if source in self.tasks:
            return self.tasks[source]
        if len(self.tasks) >= 4:
            raise CatalogError(0, "Radio metadata busy; retry later")
        task = self.tasks[source] = asyncio.create_task(self.fetch(source))

        def finished(done):
            if self.tasks.get(source) is done:
                del self.tasks[source]
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
            source = self.catalog.read(song_id, "song")["source"]
            if source in self.cache and self.cache[source][0] > now - TTL:
                return
            self.warmed[user_id] = now
            self.warmed.move_to_end(user_id)
            while len(self.warmed) > 128:
                self.warmed.popitem(last=False)
            self.task(source)
        except CatalogError, sqlite3.Error, OSError:
            # A best-effort metadata warm must not prevent playback.
            logger.debug("Radio metadata warm skipped")

    def snapshot(self, user):
        with self.accounts.connection() as connection:
            row = connection.execute(
                "SELECT * FROM radio WHERE user_id=?", (user["id"],)
            ).fetchone()
        if row and row["target"] == (user["lastfm_name"] or "").casefold():
            return dict(row)
        return None

    def filter(self, user, seed, songs):
        with self.accounts.connection() as connection:
            recent = set()
            ids = {seed["id"]}
            # Include starts without treating them as completed listening.
            for row in connection.execute(
                "SELECT song_id FROM scrobbles "
                "WHERE user_id=? AND received>=? "
                "UNION SELECT song_id FROM playback_starts "
                "WHERE user_id=? AND timestamp>=?",
                (
                    user["id"],
                    time.time() - 86400,
                    user["id"],
                    time.time() - 86400,
                ),
            ):
                ids.add(row[0])
                document = self.catalog.read(row[0], "song")["document"]
                recent.add(
                    recording_key(
                        document.get("artist", ""), document["title"]
                    )
                )
            removed = {
                r[0]
                for r in connection.execute(
                    "SELECT recording_key FROM love_removals WHERE user_id=?",
                    (user["id"],),
                )
            }
            ids |= {
                r[0]
                for r in connection.execute(
                    "SELECT item_id FROM favorite_removals WHERE user_id=?",
                    (user["id"],),
                )
            }
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
            with self.accounts.connection() as connection:
                row = connection.execute(
                    "SELECT id FROM catalog WHERE kind='song' "
                    "AND json_extract(document,'$.artistId')=? "
                    "ORDER BY complete DESC,updated DESC,id LIMIT 1",
                    (item_id,),
                ).fetchone()
            if row:
                return self.catalog.read(row[0], "song")["document"]
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

    def publish(self, user, seed, songs, generation):
        if self.generations.get(user["id"]) != generation:
            return
        target = (user["lastfm_name"] or "").casefold()
        now = time.time()
        with self.accounts.transaction() as connection:
            current = connection.execute(
                "SELECT lastfm_name FROM users WHERE id=?", (user["id"],)
            ).fetchone()
            if not current or (current[0] or "").casefold() != target:
                return
            connection.execute(
                "INSERT INTO radio VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET target=excluded.target,"
                "seed_id=excluded.seed_id,songs=excluded.songs,"
                "updated=excluded.updated",
                (
                    user["id"],
                    target,
                    seed["id"],
                    json.dumps([s["id"] for s in songs]),
                    now,
                    now,
                ),
            )

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
                source = self.catalog.read(seed["id"], "song")["source"]
                cached = self.cache.get(source)
                if cached and cached[0] > time.monotonic() - TTL:
                    self.cache.move_to_end(source)
                    candidates = cached[1]
                else:
                    candidates = await asyncio.shield(self.task(source))
                result = self.filter(user, seed, candidates)
                # Exhausted pool: allow a fresh YouTube radio at most once a
                # minute, rather than returning the same recently played songs.
                if not result and cached and cached[0] < time.monotonic() - 60:
                    candidates = await asyncio.shield(self.task(source))
                    result = self.filter(user, seed, candidates)
                if not result:
                    raise CatalogError(
                        70, "No unplayed radio tracks available"
                    )
                self.publish(user, seed, candidates, generation)
                return self.library.decorate(user["id"], result[:count])
        except CatalogError, TimeoutError, sqlite3.Error, OSError:
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
