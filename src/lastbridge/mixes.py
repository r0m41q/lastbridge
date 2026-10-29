import asyncio
import json
import logging
import random
import sqlite3
import time
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from lastbridge.catalog import CatalogError
from lastbridge.lastfm import LastfmRequestError
from lastbridge.providers.base import ProviderError
from lastbridge.recordings import recording_key

logger = logging.getLogger(__name__)
POLICIES = {
    "comfort": ("Comfort", "Familiar favorites and six-month top tracks."),
    "discover": (
        "Discover",
        "Similar tracks outside your known listening sample; charts "
        "for new profiles.",
    ),
    "again": (
        "Listen Again",
        "Recent local reports followed by Last.fm listening.",
    ),
    "loved": (
        "Loved Tracks",
        "Matched Last.fm loves still saved in your library.",
    ),
}
DAY = 86400
COOLDOWN = 300
LOOKUPS = 40
DEADLINE = 180


def key(row):
    return recording_key(*row)


def unique(rows):
    seen, result = set(), []
    for row in rows:
        identity = key(row)
        if identity not in seen:
            seen.add(identity)
            result.append(tuple(row))
    return result


def date(stamp):
    return (
        datetime.fromtimestamp(stamp, UTC).isoformat().replace("+00:00", "Z")
    )


class Mixes:
    def __init__(self, accounts, catalog, library, lastfm, client):
        self.accounts, self.catalog, self.library = accounts, catalog, library
        self.lastfm, self.client = lastfm, client
        self.task = None
        self.wake = asyncio.Event()

    def linked(self, user_id):
        with self.accounts.transaction() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if user is None or not user["lastfm_name"]:
                return
            connection.execute(
                "INSERT INTO mixes(user_id,target,created) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET target=excluded.target,"
                "revision=revision+1,running=0,next_attempt=0,requested=0,"
                "updated=0,error=NULL,profile='{}',snapshot='{}' "
                "WHERE target!=excluded.target",
                (user_id, user["lastfm_name"].casefold(), time.time()),
            )
        self.wake.set()

    async def start(self):
        with self.accounts.transaction() as connection:
            connection.execute(
                "UPDATE mixes SET running=0,next_attempt=?,"
                "error='Refresh interrupted; previous playlists "
                "kept.' WHERE running=1",
                (time.time() + 60,),
            )
            ids = [
                r[0]
                for r in connection.execute(
                    "SELECT id FROM users WHERE lastfm_name IS NOT NULL"
                )
            ]
        for user_id in ids:
            self.linked(user_id)
        self.task = asyncio.create_task(self.worker())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def worker(self):
        while True:
            self.wake.clear()
            try:
                worked = await self.refresh_one()
            except sqlite3.Error, OSError:
                logger.warning("Mix storage unavailable; retrying later")
                # Recover a claim if publication failed, keeping old snapshots.
                try:
                    with self.accounts.transaction() as connection:
                        connection.execute(
                            "UPDATE mixes SET "
                            "running=0,next_attempt=? WHERE running=1",
                            (time.time() + COOLDOWN,),
                        )
                except sqlite3.Error, OSError:
                    pass
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=1)
                except TimeoutError:
                    pass

    def request(self, user_id):
        self.linked(user_id)
        now = time.time()
        with self.accounts.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM mixes WHERE user_id=?", (user_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Link Last.fm first.")
            if row["running"] or row["requested"] > now - COOLDOWN:
                raise ValueError(
                    "Refresh already queued or requested within the "
                    "last five minutes."
                )
            connection.execute(
                "UPDATE mixes SET next_attempt=0,requested=? WHERE user_id=?",
                (now, user_id),
            )
        self.wake.set()

    def status(self, user_id):
        with self.accounts.connection() as connection:
            row = connection.execute(
                "SELECT * FROM mixes WHERE user_id=?",
                (user_id,),
            ).fetchone()
        user = self.user(user_id)
        return (
            dict(row)
            if row
            and user
            and row["target"] == (user["lastfm_name"] or "").casefold()
            else None
        )

    def user(self, user_id):
        with self.accounts.connection() as connection:
            return connection.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()

    def local(self, user_id, target):
        with self.accounts.connection() as connection:
            saved = [
                json.loads(r[0])
                for r in connection.execute(
                    "SELECT c.document FROM favorite_tracks t JOIN "
                    "catalog c ON c.id=t.song_id "
                    "JOIN favorites f ON f.user_id=t.user_id AND "
                    "f.item_id=t.favorite_id "
                    "WHERE t.user_id=? GROUP BY c.id ORDER BY "
                    "max(f.starred) DESC,c.id LIMIT 100",
                    (user_id,),
                )
            ]
            recent = [
                json.loads(r[0])
                for r in connection.execute(
                    "SELECT c.document FROM scrobbles s JOIN catalog "
                    "c ON c.id=s.song_id "
                    "WHERE s.user_id=? AND s.target=? AND s.duration>30 "
                    "GROUP BY c.id ORDER BY max(s.timestamp) "
                    "DESC,c.id LIMIT 50",
                    (user_id, target),
                )
            ]
            loved = [
                json.loads(r[0])
                for r in connection.execute(
                    "SELECT c.document FROM loved_items l JOIN "
                    "catalog c ON c.id=l.song_id "
                    "JOIN favorites f ON f.user_id=l.user_id AND "
                    "f.item_id=l.song_id "
                    "WHERE l.user_id=? AND l.target=? "
                    "AND l.state IN ('matched','skipped') "
                    "GROUP BY c.id ORDER BY max(f.starred) DESC,c.id",
                    (user_id, target),
                )
            ]
            known = {
                r[0]
                for r in connection.execute(
                    "SELECT recording_key FROM loved_items WHERE "
                    "user_id=? AND target=?",
                    (user_id, target),
                )
            }
            for r in connection.execute(
                "SELECT DISTINCT artist,title FROM scrobbles WHERE "
                "user_id=? AND target=?",
                (user_id, target),
            ):
                known.add(key(r))
            # Include all retained songs, even beyond the bounded seed sample.
            for r in connection.execute(
                "SELECT DISTINCT c.document FROM favorite_tracks f "
                "JOIN catalog c ON c.id=f.song_id WHERE f.user_id=?",
                (user_id,),
            ):
                song = json.loads(r[0])
                known.add(key((song.get("artist", ""), song["title"])))
            removed = {
                r[0]
                for r in connection.execute(
                    "SELECT recording_key FROM love_removals WHERE user_id=?",
                    (user_id,),
                )
            }
            removed_ids = {
                r[0]
                for r in connection.execute(
                    "SELECT item_id FROM favorite_removals WHERE user_id=?",
                    (user_id,),
                )
            }
        return saved, recent, loved, known, removed, removed_ids

    def playlists(self, user, item_id=None):
        row = self.status(user["id"])
        snapshot = json.loads(row["snapshot"]) if row else {}
        target = (user["lastfm_name"] or "").casefold()
        _, _, loved, known, removed, removed_ids = self.local(
            user["id"], target
        )
        result = []
        for kind, (name, description) in POLICIES.items():
            playlist_id = (
                "p"
                + uuid5(
                    NAMESPACE_URL, f"lastbridge:mix:{user['id']}:{kind}"
                ).hex
            )
            if item_id is not None and item_id != playlist_id:
                continue
            songs = (
                loved
                if kind == "loved"
                else [
                    self.catalog.read(s, "song")["document"]
                    for s in snapshot.get(kind, [])
                ]
            )
            seen, tracks = set(), []
            profile = json.loads(row["profile"]) if row else {}
            excluded = known | {key(r) for r in profile.get("familiar", [])}
            for song in songs:
                recording = key((song.get("artist", ""), song["title"]))
                if (
                    recording in seen
                    or recording in removed
                    or song["id"] in removed_ids
                ):
                    continue
                if kind == "discover" and recording in excluded:
                    continue
                seen.add(recording)
                tracks.append(song)
            stamp = row["created"] if row else 0
            changed = (
                max(
                    row["created"],
                    row["updated"],
                    user["library_changed"] / 1000,
                )
                if row
                else stamp
            )
            document = {
                "id": playlist_id,
                "name": name,
                "owner": user["username"],
                "public": False,
                "readonly": True,
                "comment": description + " Server-managed; read only.",
                "created": date(stamp),
                "changed": date(changed),
                "songCount": len(tracks),
                "duration": sum(s.get("duration", 0) for s in tracks),
            }
            if tracks and tracks[0].get("coverArt"):
                document["coverArt"] = tracks[0]["coverArt"]
            if item_id is not None:
                document["entry"] = self.library.decorate(user["id"], tracks)
            result.append(document)
        if item_id is not None:
            if not result:
                raise CatalogError(70, "Playlist not found")
            return result[0]
        return result

    def active(self, connection, job):
        row = connection.execute(
            "SELECT m.target,m.revision,u.lastfm_name,u.link_status "
            "FROM mixes m "
            "JOIN users u ON u.id=m.user_id WHERE m.user_id=?",
            (job["user_id"],),
        ).fetchone()
        return bool(
            row
            and row["revision"] == job["revision"]
            and row["target"] == job["target"]
            and row["target"] == (row["lastfm_name"] or "").casefold()
            and row["link_status"] == "linked"
        )

    async def refresh_one(self):
        if not self.lastfm.configured:
            return False
        with self.accounts.transaction() as connection:
            row = connection.execute(
                "SELECT m.* FROM mixes m JOIN users u ON u.id=m.user_id "
                "WHERE m.running=0 AND m.next_attempt<=? AND "
                "u.link_status='linked' "
                "AND u.lastfm_secret IS NOT NULL ORDER BY "
                "m.next_attempt,m.user_id LIMIT 1",
                (time.time(),),
            ).fetchone()
            if row is None:
                return False
            job = dict(row)
            if not self.active(connection, job):
                # A changed link is initialized on callback or next startup.
                connection.execute(
                    "UPDATE mixes SET next_attempt=? WHERE user_id=?",
                    (time.time() + COOLDOWN, job["user_id"]),
                )
                return True
            connection.execute(
                "UPDATE mixes SET running=1,error=NULL WHERE user_id=?",
                (job["user_id"],),
            )
        try:
            async with asyncio.timeout(DEADLINE):
                profile, snapshot = await self.build(job)
        except LastfmRequestError, CatalogError, ProviderError, TimeoutError:
            with self.accounts.transaction() as connection:
                if self.active(connection, job):
                    connection.execute(
                        "UPDATE mixes SET "
                        "running=0,next_attempt=?,error=? WHERE "
                        "user_id=?",
                        (
                            time.time() + COOLDOWN,
                            "Refresh unavailable; previous playlists "
                            "kept. Retrying in five minutes.",
                            job["user_id"],
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE mixes SET running=0 WHERE user_id=? "
                        "AND revision=?",
                        (job["user_id"], job["revision"]),
                    )
            return True
        with self.accounts.transaction() as connection:
            if self.active(connection, job):
                connection.execute(
                    "UPDATE mixes SET "
                    "running=0,next_attempt=?,updated=?,error=NULL,"
                    "profile=?,snapshot=? WHERE user_id=?",
                    (
                        time.time() + DAY,
                        time.time(),
                        json.dumps(profile),
                        json.dumps(snapshot),
                        job["user_id"],
                    ),
                )
            else:
                connection.execute(
                    "UPDATE mixes SET running=0 WHERE user_id=? AND "
                    "revision=?",
                    (job["user_id"], job["revision"]),
                )
        return True

    async def build(self, job):
        user_id, target = job["user_id"], job["target"]
        saved, local_recent, loved, known, removed, removed_ids = self.local(
            user_id, target
        )

        async def read(method, **params):
            try:
                result = await self.lastfm.taste_rows(
                    self.client, method, **params
                )
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
