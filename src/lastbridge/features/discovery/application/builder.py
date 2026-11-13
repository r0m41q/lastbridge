import asyncio
import json
import logging
import random
import time

from lastbridge.features.discovery.application.candidates import discover
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

logger = logging.getLogger(__name__)
MATCH_TIMEOUT = 5
RESOLUTION_BUDGET = 90


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
                    "artist.getSimilar",
                ) and failure.code in (6, 7):
                    # A missing seed does not invalidate a profile.
                    result = []
                else:
                    logger.warning(
                        "Mix Last.fm read failed user_id=%s method=%s code=%s",
                        user_id,
                        method,
                        failure.code,
                    )
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
        discovery = await discover(read, seeds, artists, known | removed)
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
        positions = dict.fromkeys(candidates, 0)
        deadline = time.monotonic() + RESOLUTION_BUDGET
        for _ in range(60):
            # Discover needs new lookups; familiar mixes often use the cache.
            # Give it two turns while retaining a single shared hard bound.
            for kind in ("discover", "comfort", "discover", "again"):
                rows = candidates[kind]
                i = positions[kind]
                positions[kind] += 1
                if i >= len(rows) or len(results[kind]) >= 30:
                    continue
                candidate = rows[i]
                recording = key(candidate)
                if recording in removed:
                    continue
                if recording not in cache:
                    remaining = deadline - time.monotonic()
                    if attempts >= LOOKUPS or remaining <= 0:
                        continue
                    attempts += 1
                    try:
                        async with asyncio.timeout(
                            min(MATCH_TIMEOUT, remaining)
                        ):
                            cache[recording] = await self.catalog.match_loved(
                                *candidate
                            )
                    except ProviderError, TimeoutError:
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
        logger.info(
            "Mix matching completed user_id=%s lookups=%s "
            "comfort=%s discover=%s again=%s discover_candidates=%s",
            user_id,
            attempts,
            len(results["comfort"]),
            len(results["discover"]),
            len(results["again"]),
            len(discovery),
        )
        if not results["discover"]:
            logger.warning(
                "Discover empty user_id=%s reason=%s",
                user_id,
                "no_playable_matches" if discovery else "no_new_suggestions",
            )
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
