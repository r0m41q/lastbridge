import logging
import re

from lastbridge.features.playlists.domain.matching import (
    compare,
    normalize,
    title_parts,
)

logger = logging.getLogger(__name__)


class PlaylistResolver:
    def __init__(self, fetch, song):
        self.fetch, self.song = fetch, song

    async def resolve(self, source):
        source = source | {
            "artists": [
                a
                for a in source["artists"]
                if isinstance(a, dict) and isinstance(a.get("name"), str)
            ]
        }
        names = [a["name"] for a in source["artists"]]
        base, versions, _ = title_parts(source["title"], names)
        title = " ".join([base, *versions])
        queries = list(
            dict.fromkeys(
                [
                    f"{names[0]} {title}",
                    f"{names[0]} {source['title']}",
                    f"{' '.join(names)} {title}",
                ]
            )
        )
        rows = {}
        for query in queries:
            result = await self.fetch(
                {
                    "operation": "search",
                    "query": query,
                    "kinds": ["songs"],
                }
            )
            for row in result.get("songs", [])[:100]:
                if (
                    isinstance(row, dict)
                    and row.get("videoType") == "MUSIC_VIDEO_TYPE_ATV"
                    and isinstance(row.get("videoId"), str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{11}", row["videoId"])
                    and row.get("isAvailable") is not False
                    and isinstance(row.get("title"), str)
                    and isinstance(row.get("artists"), list)
                    and isinstance(row.get("album"), dict)
                    and isinstance(row["album"].get("id"), str)
                    and row["album"]["id"].startswith("MPRE")
                    and isinstance(row["album"].get("name"), str)
                    and all(
                        isinstance(a, dict) and isinstance(a.get("name"), str)
                        for a in row["artists"]
                    )
                ):
                    rows.setdefault(row.get("videoId"), row)
            if self.eligible(source, list(rows.values())):
                break
        eligible = self.eligible(source, list(rows.values()))
        groups = set()
        for row in eligible:
            duration = row.get("duration_seconds")
            if isinstance(duration, (int, float)):
                groups.add(duration)
        ambiguous = bool(groups and max(groups) - min(groups) > 3)
        selected = eligible[0] if eligible and not ambiguous else None
        candidates = []
        ordered = sorted(
            rows.values(),
            key=lambda r: compare(source, r)["score"],
            reverse=True,
        )
        for row in ordered:
            flags = compare(source, row)
            if not flags["exact"] and not (
                flags["artist_ok"]
                and flags["version_ok"]
                and flags["score"] >= 0.88
            ):
                continue
            song = self.song(row)
            if song is None:
                continue
            candidates.append(
                {
                    "song_id": song["id"],
                    "source": row["videoId"],
                    "title": row["title"],
                    "artist": ", ".join(a["name"] for a in row["artists"]),
                    "album": row["album"].get("name"),
                    "duration": row.get("duration_seconds"),
                    "explicit": row.get("isExplicit"),
                    "mismatch": not all(
                        flags[k]
                        for k in (
                            "artist_ok",
                            "version_ok",
                            "explicit_ok",
                        )
                    ),
                    "score": round(flags["score"], 3),
                }
            )
            if len(candidates) == 5:
                break
        song = self.song(selected) if selected else None
        outcome = (
            "matched"
            if song
            else "review_required"
            if candidates
            else "no_candidate"
        )
        reason = "No audio candidate"
        if candidates:
            flags = compare(
                source,
                next(
                    r
                    for r in rows.values()
                    if r["videoId"] == candidates[0]["source"]
                ),
            )
            reason = (
                "Artist mismatch"
                if not flags["artist_ok"]
                else "Version mismatch"
                if not flags["version_ok"]
                else "Explicitness mismatch"
                if not flags["explicit_ok"]
                else "Multiple recording versions"
                if ambiguous
                else "Review required"
            )
        logger.info(
            "Playlist resolution outcome=%s candidates=%s selected=%s",
            outcome,
            len(candidates),
            selected.get("videoId") if selected else "-",
        )
        return {
            "song": song,
            "candidates": candidates,
            "outcome": outcome,
            "reason": reason,
        }

    @staticmethod
    def eligible(source, rows):
        matches = []
        for row in rows:
            flags = compare(source, row)
            if all(
                flags[k]
                for k in (
                    "artist_ok",
                    "version_ok",
                    "explicit_ok",
                    "exact",
                )
            ):
                matches.append(row)
        matches.sort(
            key=lambda r: (
                0
                if r.get("isExplicit") is True
                else 2
                if r.get("isExplicit") is False
                else 1,
            )
        )
        if matches and source.get("isExplicit") is None:
            preferred = matches[0].get("isExplicit")
            matches = [r for r in matches if r.get("isExplicit") is preferred]
        # Different performer sets are not equivalent album editions.
        if matches:
            credits = {
                tuple(sorted(normalize(a["name"]) for a in r["artists"]))
                for r in matches
            }
            if len(credits) > 1:
                return []
        return matches
