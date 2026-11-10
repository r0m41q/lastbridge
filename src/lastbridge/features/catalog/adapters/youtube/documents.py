import re

from lastbridge.features.catalog.adapters.youtube.radio import RadioDocuments
from lastbridge.features.catalog.domain.documents import (
    SOURCE,
    number,
    thumbnail,
)
from lastbridge.features.catalog.domain.matching import match_recording
from lastbridge.features.catalog.domain.models import Candidate, Recording
from lastbridge.shared.errors import CatalogError


class MetadataDocuments(RadioDocuments):
    """Normalize YouTube payloads and persist canonical catalog documents."""

    def __init__(self, repository):
        self.repository = repository

    def put(self, *args, **kwargs):
        return self.repository.put(*args, **kwargs)

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
                **(
                    {"isExplicit": row["isExplicit"]}
                    if type(row.get("isExplicit")) is bool
                    else {}
                ),
                "coverArt": album.get("coverArt"),
                **self.credits(row),
            },
            thumbnail(row),
            complete=position is not None,
        )

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
        return songs

    def match_loved(self, rows, artist, title):
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
