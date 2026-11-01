import asyncio
import json
import random
import time

from lastbridge.features.discovery.application.ports import (
    CatalogAccess,
    MixStore,
    TasteProvider,
)
from lastbridge.features.discovery.domain.policy import (
    DAY,
    LOOKUPS,
    key,
    unique,
)
from lastbridge.shared.errors import CatalogError, ProviderError
from lastbridge.shared.lastfm import LastfmRequestError


class MixBuilder:
    def __init__(
        self,
        repository: MixStore,
        catalog: CatalogAccess,
        remote: TasteProvider,
    ):
        self.repository, self.catalog, self.remote = (
            repository,
            catalog,
            remote,
        )

    def local(self, user_id, target):
        return self.repository.local(user_id, target)

    async def build(self, job):
        user_id, target = job["user_id"], job["target"]
        saved, local_recent, loved, known, removed, removed_ids = self.local(
            user_id, target
        )

        async def read(method, **params):
            try:
                result = await self.remote.taste_rows(method, **params)
            except LastfmRequestError as failure:
                if method in (
                    "track.getSimilar",
                    "artist.getTopTracks",
                ) and failure.code in (6, 7):
                    # A missing seed does not invalidate a profile.
                    result = []
                else:
                    raise
            await asyncio.sleep(0.25)
            return result

        top = await read("user.getTopTracks", user=target, period="6month")
        artists = await read(
            "user.getTopArtists", user=target, period="6month"
        )
        recent = await read("user.getRecentTracks", user=target)

        def pairs(songs):
            return [
                (s["artist"], s["title"]) for s in songs if s.get("artist")
            ]

        familiar = unique(
            pairs(saved)
            + top
            + pairs(loved[:100])
            + pairs(local_recent)
            + recent
        )
        known |= {key(r) for r in familiar}
        rng = random.Random(f"{user_id}:{target}:{int(time.time() // DAY)}")
        seeds = list(familiar)
        rng.shuffle(seeds)
        groups = []
        for artist, title in seeds[:3]:
            groups.append(
                await read(
                    "track.getSimilar",
                    artist=artist,
                    track=title,
                    autocorrect="0",
                )
            )
        if not seeds and artists:
            for artist, _ in artists[:2]:
                groups.append(await read("artist.getTopTracks", artist=artist))
        if not seeds and not artists:
            groups.append(await read("chart.getTopTracks"))
        # Interleave seed results so the first seed cannot monopolize Discover.
        discovery = unique(
            [rows[i] for i in range(50) for rows in groups if i < len(rows)]
        )
        discovery = [r for r in discovery if key(r) not in known | removed]
        comfort = unique(pairs(saved) + top + pairs(loved[:100]))
        rng.shuffle(comfort)
        candidates = {
            "comfort": comfort[:60],
            "discover": discovery[:60],
            "again": unique(pairs(local_recent) + recent)[:50],
        }
        cache = {}
        for song in saved + local_recent + loved[:100]:
            if song.get("artist"):
                cache[key((song["artist"], song["title"]))] = song
        old_profile = json.loads(job["profile"])
        for recording, song_id in old_profile.get("resolved", {}).items():
            cache.setdefault(
                recording, self.catalog.read(song_id, "song")["document"]
            )
        results = {kind: [] for kind in candidates}
        seen = {kind: set() for kind in candidates}
        seen_ids = {kind: set() for kind in candidates}
        attempts, resolved = 0, {}
        for i in range(60):
            for kind, rows in candidates.items():
                if i >= len(rows) or len(results[kind]) >= 30:
                    continue
                candidate = rows[i]
                recording = key(candidate)
                if recording in removed:
                    continue
                if recording not in cache:
                    if attempts >= LOOKUPS:
                        continue
                    attempts += 1
                    try:
                        cache[recording] = await self.catalog.match_loved(
                            *candidate
                        )
                    except ProviderError:
                        cache[recording] = (
                            None  # Ambiguous/unavailable are omitted.
                        )
                    except CatalogError as failure:
                        if failure.code != 70:
                            raise
                        cache[recording] = None
                    await asyncio.sleep(0.25)
                song = cache[recording]
                if song is None or song["id"] in removed_ids:
                    continue
                actual = key((song.get("artist", ""), song["title"]))
                if (
                    actual in removed
                    or actual in seen[kind]
                    or song["id"] in seen_ids[kind]
                ):
                    continue
                if kind == "discover" and actual in known:
                    continue
                seen[kind].add(actual)
                seen_ids[kind].add(song["id"])
                results[kind].append(song["id"])
                resolved[recording] = song["id"]
        # A provider returning only unresolvable metadata must not erase a
        # previously useful snapshot. Empty remote histories are valid results.
        old_snapshot = json.loads(job["snapshot"])
        if any(
            rows and not results[kind] and old_snapshot.get(kind)
            for kind, rows in candidates.items()
        ):
            raise CatalogError()
        return {
            "top": top,
            "artists": [a for a, _ in artists],
            "recent": recent,
            "familiar": familiar,
            "candidates": candidates,
            "resolved": resolved,
        }, results
