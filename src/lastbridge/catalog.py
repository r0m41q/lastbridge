import asyncio
import contextlib
import json
import re
import sys
import time
from collections import OrderedDict
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx

from lastbridge.accounts import Accounts
from lastbridge.providers.base import Candidate, Recording
from lastbridge.providers.youtube import match_recording

SOURCE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
IMAGE_HOSTS = frozenset(
    {"lh3.googleusercontent.com", "yt3.googleusercontent.com", "i.ytimg.com"}
)


class CatalogError(Exception):
    def __init__(self, code=0, message="Metadata provider unavailable"):
        self.code, self.message = code, message
        super().__init__(message)


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
    for item in reversed(row.get("thumbnails") or []):
        if url := image_url(item.get("url")):
            return url
    return None


def number(value):
    try:
        result = int(value)
        return result if 0 <= result <= 2**31 - 1 else None
    except ValueError, TypeError, OverflowError:
        return None


class MetadataProvider:
    def __init__(self, timeout=30):
        self.timeout = timeout
        self.slots = asyncio.Semaphore(2)
        # Artist pages fan out into album requests. Reserve independent bounded
        # capacity for the foreground radio action so those cannot starve it.
        self.radio_slots = asyncio.Semaphore(2)

    async def exchange(self, process, payload):
        process.stdin.write(json.dumps(payload).encode())
        await process.stdin.drain()
        process.stdin.close()
        output = bytearray()
        while chunk := await process.stdout.read(64 * 1024):
            output.extend(chunk)
            if len(output) > 8 * 1024 * 1024:
                raise CatalogError(0, "Metadata response exceeds size limit")
        await process.wait()
        return output

    async def fetch(self, payload):
        slots = (
            self.radio_slots
            if payload.get("operation") == "radio"
            else self.slots
        )
        try:
            await asyncio.wait_for(slots.acquire(), timeout=1)
        except TimeoutError:
            raise CatalogError(
                0, "Metadata provider busy; retry shortly"
            ) from None
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "lastbridge.providers.catalog_worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output = await asyncio.wait_for(
                self.exchange(process, payload), timeout=self.timeout
            )
            result = json.loads(output)
            if (
                process.returncode
                or not isinstance(result, dict)
                or "error" in result
                or not isinstance(result.get("data"), dict)
            ):
                raise CatalogError()
            return result["data"]
        except TimeoutError, ValueError, KeyError, OSError:
            raise CatalogError() from None
        finally:
            if process is not None:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                # Drain the killed worker's pipes; wait() alone can deadlock
                # when unread stdout has paused the subprocess transport.
                await process.communicate()
            slots.release()


def client_document(kind, document):
    """Make persisted pre-rendition metadata safe for client song parsers."""
    document = dict(document)
    if kind == "song":
        # Feishin unconditionally calls contentType.startsWith(). Unknown media
        # needs a string without inventing an audio format before resolution.
        document.setdefault("contentType", "application/octet-stream")
    child = {"album": "song", "artist": "album"}.get(kind)
    if child and child in document:
        document[child] = [client_document(child, d) for d in document[child]]
    return document


class Catalog:
    def __init__(self, accounts: Accounts, provider=None):
        self.accounts = accounts
        self.provider = (
            provider if provider is not None else MetadataProvider()
        )
        # Short-lived, bounded, per-user search snapshots stabilize pagination.
        # Queries are never persisted in the shared metadata database.
        self.searches = OrderedDict()
        self.artist_tops = OrderedDict()

    def remember_tops(self, artist, row):
        songs, seen = [], set()
        group = row.get("songs") or {}
        if not isinstance(group, dict):
            raise CatalogError()
        rows = group.get("results", [])
        if not isinstance(rows, list):
            raise CatalogError()
        for track in rows[:50]:
            if not isinstance(track, dict):
                continue
            album = track.get("album")
            if (
                not isinstance(track.get("videoId"), str)
                or not isinstance(track.get("title"), str)
                or not 0 < len(track["title"]) <= 1024
                or not isinstance(album, dict)
                or not isinstance(album.get("id"), str)
                or not SOURCE.fullmatch(album["id"])
                or not isinstance(album.get("name"), str)
                or not album["name"]
            ):
                continue
            credits = track.get("artists")
            if not isinstance(credits, list) or not any(
                isinstance(a, dict) and a.get("id") == artist["source"]
                for a in credits
            ):
                continue
            if any(
                not isinstance(a, dict) or not isinstance(a.get("name"), str)
                for a in credits
            ):
                continue
            credits = [
                a
                | {
                    "id": a.get("id")
                    if isinstance(a.get("id"), str)
                    and SOURCE.fullmatch(a["id"])
                    else None
                }
                for a in credits
            ]
            duration = number(track.get("duration_seconds"))
            length = track.get("duration") or track.get("length")
            if (
                duration is None
                and isinstance(length, str)
                and re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", length)
            ):
                parts = [int(p) for p in length.split(":")]
                if all(p < 60 for p in parts[1:]):
                    duration = 0
                    for part in parts:
                        duration = duration * 60 + part
            song = self.song(
                track | {"duration_seconds": duration, "artists": credits}
            )
            if song and song["id"] not in seen:
                songs.append(song)
                seen.add(song["id"])
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
            with self.accounts.connection() as connection:
                candidates = [
                    dict(r)
                    for r in connection.execute(
                        "SELECT * FROM catalog WHERE kind='artist'"
                    )
                    if json.loads(r["document"])["name"].strip().casefold()
                    == name
                ]
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

    def read(self, item_id, kind=None):
        with self.accounts.connection() as connection:
            row = connection.execute(
                "SELECT * FROM catalog WHERE id=?", (item_id,)
            ).fetchone()
        if row is None or (kind is not None and row["kind"] != kind):
            raise CatalogError(70, "Catalog item not found")
        return dict(row) | {
            "document": client_document(
                row["kind"], json.loads(row["document"])
            )
        }

    def put(self, kind, source, document, image=None, complete=False):
        if not isinstance(source, str) or not SOURCE.fullmatch(source):
            raise CatalogError(70, "Catalog item unavailable")
        item_id = identity(kind, source)
        document = {k: v for k, v in document.items() if v is not None}
        document["id"] = item_id
        if image:
            document["coverArt"] = item_id
        with self.accounts.transaction() as connection:
            old = connection.execute(
                "SELECT document,image,complete FROM catalog WHERE id=?",
                (item_id,),
            ).fetchone()
            if old:
                # Sparse search rows must not erase hydrated album/song data.
                prior = json.loads(old["document"])
                document = (
                    document | prior
                    if old["complete"] and not complete
                    else prior | document
                )
                image = image or old["image"]
                complete = complete or bool(old["complete"])
            if kind == "album":
                document["created"] = (
                    json.loads(old["document"]).get("created") if old else None
                ) or datetime.now(UTC).isoformat().replace("+00:00", "Z")
            document = client_document(kind, document)
            connection.execute(
                "INSERT INTO catalog VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET document=excluded.document,"
                "image=excluded.image,complete=excluded.complete,"
                "updated=excluded.updated",
                (
                    item_id,
                    kind,
                    source,
                    json.dumps(document),
                    image,
                    int(complete),
                    int(time.time()),
                ),
            )
        return document

    def artist(self, row):
        source = row.get("id") or row.get("browseId")
        name = row.get("name") or row.get("artist")
        if not source or not name:
            return None
        return self.put(
            "artist", source, {"name": name, "albumCount": 0}, thumbnail(row)
        )

    def credits(self, row):
        artists = row.get("artists") or []
        names = [a["name"] for a in artists if a.get("name")]
        first = self.artist(artists[0]) if artists else None
        return {
            "artist": ", ".join(names) or None,
            "artistId": first["id"] if first else None,
        }

    def album(self, row):
        source = row.get("browseId") or row.get("id")
        name = row.get("title") or row.get("name")
        if not source or not source.startswith("MPRE") or not name:
            return None
        return self.put(
            "album",
            source,
            {
                "name": name,
                "songCount": 0,
                "duration": 0,
                "year": number(row.get("year")),
                **self.credits(row),
            },
            thumbnail(row),
        )

    def song(self, row, album=None, position=None):
        source, title = row.get("videoId"), row.get("title")
        if (
            not source
            or not re.fullmatch(r"[A-Za-z0-9_-]{11}", source)
            or not title
            or row.get("isAvailable") is False
        ):
            return None
        if album is None:
            if row.get("videoType") != "MUSIC_VIDEO_TYPE_ATV":
                return None
            album_row = row.get("album")
            if not isinstance(album_row, dict):
                return None
            album = self.album(album_row | {"artists": row.get("artists")})
        if album is None:
            return None
        return self.put(
            "song",
            source,
            {
                "title": title,
                "isDir": False,
                "isVideo": False,
                "type": "music",
                "album": album["name"],
                "albumId": album["id"],
                "parent": album["id"],
                "year": album.get("year"),
                "track": position,
                "duration": number(row.get("duration_seconds")),
                "coverArt": album.get("coverArt"),
                **self.credits(row),
            },
            thumbnail(row),
            complete=position is not None,
        )

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
        # No album/duration is supplied by user.getLovedTracks. Keep version
        # qualifiers and reject multiple distinct eligible recordings.
        rows = await self.provider.fetch(
            {
                "operation": "search",
                "query": f"{artist} {title}",
                "kinds": ["songs"],
            }
        )
        candidates, originals = [], {}
        for row in rows.get("songs", [])[:100]:
            if not isinstance(row, dict):
                continue
            source = row.get("videoId")
            if (
                not isinstance(source, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{11}", source)
                or row.get("videoType") != "MUSIC_VIDEO_TYPE_ATV"
                or row.get("isAvailable") is False
                or not isinstance(row.get("album"), dict)
                or not isinstance(row.get("title"), str)
                or not isinstance(row.get("artists"), list)
            ):
                continue
            names = tuple(
                a["name"]
                for a in row["artists"]
                if isinstance(a, dict) and isinstance(a.get("name"), str)
            )
            candidates.append(
                Candidate(
                    source,
                    row.get("title", ""),
                    names,
                    row["album"].get("name"),
                    row.get("duration_seconds"),
                    True,
                )
            )
            originals[source] = row
        match = match_recording(Recording(artist, title), candidates)
        song = self.song(originals[match.source_id])
        if song is None:
            raise CatalogError(70, "Recording unavailable")
        return song

    async def radio(self, source):
        result = await self.provider.fetch(
            {"operation": "radio", "source": source}
        )
        rows = result.get("tracks")
        if not isinstance(rows, list):
            raise CatalogError()
        songs, seen = [], set()
        for row in rows[:100]:
            if not isinstance(row, dict):
                continue
            album, artists = row.get("album"), row.get("artists")
            if (
                row.get("videoType") != "MUSIC_VIDEO_TYPE_ATV"
                or not isinstance(row.get("videoId"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{11}", row["videoId"])
                or row.get("isAvailable") is False
                or not isinstance(album, dict)
                or not isinstance(album.get("id"), str)
                or not SOURCE.fullmatch(album["id"])
                or not album["id"].startswith("MPRE")
                or not isinstance(album.get("name"), str)
                or not album["name"]
                or not isinstance(row.get("title"), str)
                or not 0 < len(row["title"]) <= 1024
                or not isinstance(artists, list)
            ):
                continue
            credits = [
                a
                for a in artists
                if isinstance(a, dict)
                and isinstance(a.get("name"), str)
                and a["name"]
            ]
            if not credits:
                continue
            duration = number(row.get("duration_seconds"))
            if duration is None:
                length = row.get("length", "")
                if not isinstance(length, str) or not re.fullmatch(
                    r"\d{1,2}:\d{2}(?::\d{2})?", length
                ):
                    continue
                fields = [int(p) for p in length.split(":")]
                if any(p >= 60 for p in fields[1:]):
                    continue
                duration = 0
                for part in fields:
                    duration = duration * 60 + part
            if not 30 < duration < 86400:
                continue
            thumbnails = row.get("thumbnail")
            thumbnails = (
                [t for t in thumbnails if isinstance(t, dict)]
                if isinstance(thumbnails, list)
                else []
            )
            credits = [
                a
                | {
                    "id": a.get("id")
                    if isinstance(a.get("id"), str)
                    and SOURCE.fullmatch(a["id"])
                    else None
                }
                for a in credits
            ]
            song = self.song(
                row
                | {
                    "artists": credits,
                    "duration_seconds": duration,
                    "thumbnails": thumbnails,
                }
            )
            if song and song["id"] not in seen:
                songs.append(song)
                seen.add(song["id"])
            if len(songs) == 50:
                break
        return songs

    async def detail(self, item_id, kind):
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
        else:
            try:
                self.remember_tops(stored, row)
            except CatalogError:
                # Optional top songs must not break album enumeration.
                pass
            albums = []
            seen = set()
            for group in ("albums", "singles"):
                for release in (row.get(group) or {}).get("results", [])[:100]:
                    album = self.album(
                        release
                        | {
                            "artists": [
                                {
                                    "id": stored["source"],
                                    "name": document["name"],
                                }
                            ]
                        }
                    )
                    if album and album["id"] not in seen:
                        albums.append(album)
                        seen.add(album["id"])
            document = document | {
                "name": row.get("name") or document["name"],
                "album": albums,
                "albumCount": len(albums),
            }
        return self.put(
            kind, stored["source"], document, thumbnail(row), complete=True
        )

    async def artwork(self, client: httpx.AsyncClient, item_id):
        stored = self.read(item_id)
        url = image_url(stored["image"])
        if url is None:
            # Song search may have only its album's image.
            parent = stored["document"].get("albumId")
            if parent:
                return await self.artwork(client, parent)
            raise CatalogError(70, "Artwork unavailable")
        try:
            async with client.stream("GET", url, follow_redirects=False) as r:
                r.raise_for_status()
                media_type = r.headers.get("content-type", "").split(";")[0]
                if media_type not in ("image/jpeg", "image/png", "image/webp"):
                    raise CatalogError(70, "Artwork unavailable")
                data = bytearray()
                async for chunk in r.aiter_bytes(chunk_size=64 * 1024):
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024:
                        raise CatalogError(0, "Artwork exceeds size limit")
                if not data:
                    raise CatalogError(70, "Artwork unavailable")
                return bytes(data), media_type
        except httpx.HTTPError:
            raise CatalogError(0, "Artwork provider unavailable") from None
