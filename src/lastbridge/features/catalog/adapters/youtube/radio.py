import re

from lastbridge.features.catalog.domain.documents import SOURCE, number
from lastbridge.shared.errors import CatalogError


class RadioDocuments:
    def radio(self, result):
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
