import asyncio
import time
from collections import OrderedDict
from weakref import WeakValueDictionary

from lastbridge.features.catalog.application.ports import (
    CatalogStore,
    DocumentMapping,
    MetadataPort,
)
from lastbridge.features.catalog.domain.documents import thumbnail
from lastbridge.shared.errors import CatalogError


class Catalog:
    def __init__(
        self,
        repository: CatalogStore,
        provider: MetadataPort,
        documents: DocumentMapping,
        artwork_lookup,
    ):
        self.repository = repository
        self.provider = provider
        self.documents = documents
        self.artwork_lookup = artwork_lookup
        self.searches = OrderedDict()
        self.artist_tops = OrderedDict()
        self.artist_locks = WeakValueDictionary()
        self.artist_retries = OrderedDict()

    def read(self, item_id, kind=None):
        return self.repository.read(item_id, kind)

    def read_source(self, source, kind):
        return self.repository.read_source(source, kind)

    def song_named(self, artist, title):
        return self.repository.song_named(artist, title)

    def put(self, *args, **kwargs):
        return self.repository.put(*args, **kwargs)

    def artist(self, row):
        return self.documents.artist(row)

    def album(self, row):
        return self.documents.album(row)

    def song(self, row, album=None, position=None):
        return self.documents.song(row, album, position)

    def remember_tops(self, artist, row):
        songs = self.documents.remember_tops(artist, row)
        self.artist_tops[artist["source"]] = (time.monotonic() + 900, songs)
        self.artist_tops.move_to_end(artist["source"])
        while len(self.artist_tops) > 32:
            self.artist_tops.popitem(last=False)
        return songs

    async def top_songs(self, user_id, item_id=None, name=None, *, fetch=True):
        if item_id is not None:
            artist = self.read(item_id, "artist")
        else:
            if not isinstance(name, str) or not 0 < len(name.strip()) <= 256:
                raise CatalogError(10, "Required parameter: artist")
            name = name.strip().casefold()
            candidates = self.repository.artists_named(name)
            if not candidates and fetch:
                result = await self.search(user_id, name, ["artists"])
                candidates = [
                    self.read(a["id"], "artist")
                    for a in result["artist"]
                    if a["name"].strip().casefold() == name
                ]
            if len(candidates) > 1:
                raise CatalogError(70, "Artist name is ambiguous; use its ID")
            if not candidates:
                return []
            artist = candidates[0]
        cached = self.artist_tops.get(artist["source"])
        if cached and cached[0] > time.monotonic():
            self.artist_tops.move_to_end(artist["source"])
            return cached[1]
        if not fetch:
            return []
        try:
            row = await self.provider.fetch(
                {"operation": "artist", "source": artist["source"]}
            )
            return self.remember_tops(artist, row)
        except CatalogError:
            if cached and cached[0] > time.monotonic() - 86400:
                return cached[1]
            raise

    async def search(self, user_id, query, kinds):
        key = (user_id, query, tuple(kinds))
        now = time.monotonic()
        for expired in [k for k, v in self.searches.items() if v[0] <= now]:
            del self.searches[expired]
        if key in self.searches:
            self.searches.move_to_end(key)
            return self.searches[key][1]
        rows = await self.provider.fetch(
            {"operation": "search", "query": query, "kinds": kinds}
        )
        result = {"artist": [], "album": [], "song": []}
        for plural, kind in (
            ("artists", "artist"),
            ("albums", "album"),
            ("songs", "song"),
        ):
            seen = set()
            for row in rows.get(plural, [])[:100]:
                document = getattr(self, kind)(row)
                if document and document["id"] not in seen:
                    result[kind].append(
                        {
                            k: v
                            for k, v in document.items()
                            if k not in ("album", "song") or isinstance(v, str)
                        }
                    )
                    seen.add(document["id"])
        self.searches[key] = (now + 60, result)
        while len(self.searches) > 32:
            self.searches.popitem(last=False)
        return result

    async def match_loved(self, artist, title):
        rows = await self.provider.fetch(
            {
                "operation": "search",
                "query": f"{artist} {title}",
                "kinds": ["songs"],
            }
        )
        return self.documents.match_loved(rows, artist, title)

    async def radio(self, source):
        rows = await self.provider.fetch(
            {"operation": "radio", "source": source}
        )
        return self.documents.radio(rows)

    async def detail(self, item_id, kind):
        if kind == "artist":
            lock = self.artist_locks.setdefault(item_id, asyncio.Lock())
            async with lock:
                return await self.artist_detail(item_id)
        stored = self.read(item_id, kind)
        document = stored["document"]
        if kind == "song" or stored["complete"]:
            return document
        row = await self.provider.fetch(
            {"operation": kind, "source": stored["source"]}
        )
        if kind == "album":
            row["browseId"] = stored["source"]
            album = self.album(row)
            if not album:
                raise CatalogError(70, "Album unavailable")
            songs = []
            for i, track in enumerate(row.get("tracks", [])[:1000], 1):
                if song := self.song(track, album, i):
                    songs.append(song)
            document = album | {
                "song": songs,
                "songCount": len(songs),
                "duration": sum(s.get("duration", 0) for s in songs),
            }
        return self.put(
            kind, stored["source"], document, thumbnail(row), complete=True
        )

    async def artist_detail(self, item_id):
        stored = self.read(item_id, "artist")
        document = stored["document"]
        snapshot = stored.get("snapshot") or {}
        ttl = 86400 if snapshot.get("complete") else 300
        if (
            snapshot.get("version") == 1
            and 0 <= time.time() - snapshot.get("refreshed_at", 0) < ttl
        ) or self.artist_retries.get(item_id, 0) > time.monotonic():
            return document
        try:
            row = await self.provider.fetch(
                {
                    "operation": "artist",
                    "source": stored["source"],
                    "releases": True,
                }
            )
            if not isinstance(row.get("name"), str) or not row["name"]:
                raise CatalogError()
            albums, seen = [], set()
            complete = row.get("discography_complete") is True
            for group in ("albums", "singles"):
                shelf = row.get(group) or {}
                if not isinstance(shelf, dict):
                    raise CatalogError()
                releases = shelf.get("results", [])
                if not isinstance(releases, list):
                    raise CatalogError()
                for release in releases[:1000]:
                    if not isinstance(release, dict):
                        complete = False
                        continue
                    credits = release.get("artists")
                    if not isinstance(credits, list) or not any(
                        isinstance(a, dict) and a.get("name") for a in credits
                    ):
                        credits = [
                            {"id": stored["source"], "name": row["name"]}
                        ]
                    album = self.album(
                        release
                        | {
                            "artists": credits,
                            "type": release.get("type")
                            or ("Album" if group == "albums" else "Single"),
                        }
                    )
                    if album and album["id"] not in seen:
                        albums.append(album)
                        seen.add(album["id"])
                    elif album is None:
                        complete = False
            if not complete:
                albums.extend(
                    a for a in document.get("album", []) if a["id"] not in seen
                )
            try:
                self.remember_tops(stored, row)
            except CatalogError:
                pass
            document = document | {
                "name": row["name"],
                "album": [
                    {k: v for k, v in a.items() if k != "song"} for a in albums
                ],
                "albumCount": len(albums),
            }
            self.artist_retries.pop(item_id, None)
            return self.put(
                "artist",
                stored["source"],
                document,
                thumbnail(row),
                complete=True,
                snapshot={
                    "version": 1,
                    "complete": complete,
                    "refreshed_at": time.time(),
                },
            )
        except CatalogError:
            if "album" not in document:
                raise
            self.artist_retries[item_id] = time.monotonic() + 300
            self.artist_retries.move_to_end(item_id)
            while len(self.artist_retries) > 128:
                self.artist_retries.popitem(last=False)
            return document

    async def artwork(self, client, item_id):
        return await self.artwork_lookup.fetch(client, item_id)
