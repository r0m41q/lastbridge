import asyncio
import contextlib
import logging
import sqlite3
import time
from datetime import UTC, datetime

from lastbridge.catalog import CatalogError

logger = logging.getLogger(__name__)


class Library:
    def __init__(self, accounts, catalog, policy):
        self.accounts, self.catalog, self.policy = accounts, catalog, policy
        self.audio = None
        self.wake = asyncio.Event()
        self.worker = None

    def sources(self):
        with self.accounts.connection() as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT c.source FROM favorite_tracks t "
                    "JOIN catalog c ON c.id=t.song_id"
                )
            }

    def pins(self):
        return {f"{source}-{self.policy}" for source in self.sources()}

    def stars(self, user_id):
        with self.accounts.connection() as connection:
            return dict(
                connection.execute(
                    "SELECT item_id,starred FROM favorites WHERE user_id=?",
                    (user_id,),
                ).fetchall()
            )

    def decorate(self, user_id, document):
        stars = self.stars(user_id)

        def visit(value):
            if isinstance(value, list):
                return [visit(v) for v in value]
            if not isinstance(value, dict):
                return value
            result = {k: visit(v) for k, v in value.items() if k != "starred"}
            if result.get("id") in stars:
                result["starred"] = stars[result["id"]]
            return result

        return visit(document)

    async def change(self, user_id, targets, enabled):
        if not targets:
            raise CatalogError(10, "Supply id, albumId or artistId")
        # Validate the whole batch before changing private membership. Album
        # metadata is resolved first; a failed lookup cannot half-save a batch.
        prepared = {}
        for item_id, kind in targets:
            stored = self.catalog.read(item_id, kind)
            tracks = []
            if enabled:
                if stored["kind"] == "song":
                    tracks = [item_id]
                elif stored["kind"] == "album":
                    album = await self.catalog.detail(item_id, "album")
                    tracks = [song["id"] for song in album.get("song", [])]
            prepared[item_id] = tracks
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with self.accounts.transaction() as connection:
            changed = False
            for item_id, tracks in prepared.items():
                if not enabled:
                    deleted = connection.execute(
                        "DELETE FROM favorites WHERE user_id=? AND item_id=?",
                        (user_id, item_id),
                    ).rowcount
                    changed = changed or bool(deleted)
                    continue
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO favorites VALUES(?,?,?)",
                    (user_id, item_id, now),
                ).rowcount
                if inserted:
                    changed = True
                    connection.executemany(
                        "INSERT INTO favorite_tracks VALUES(?,?,?)",
                        [(user_id, item_id, song) for song in set(tracks)],
                    )
            if changed:
                connection.execute(
                    "UPDATE users SET "
                    "library_changed=MAX(library_changed+1,?) "
                    "WHERE id=?",
                    (int(time.time() * 1000), user_id),
                )
            # Commit job intent with favorites, before any cancellable storage
            # reconciliation. Restart can recover if that later step fails.
            connection.execute(
                "INSERT OR IGNORE INTO save_jobs(source,policy,state) "
                "SELECT DISTINCT c.source,?,'pending' FROM favorite_tracks t "
                "JOIN catalog c ON c.id=t.song_id WHERE t.user_id=?",
                (self.policy, user_id),
            )
            connection.execute(
                "DELETE FROM save_jobs WHERE policy!=? OR source NOT IN "
                "(SELECT c.source FROM favorite_tracks t "
                "JOIN catalog c ON c.id=t.song_id)",
                (self.policy,),
            )
        await self.reconcile()
        self.wake.set()

    async def reconcile(self, startup=False):
        # Durable favorites are the source of pin truth. A failed promotion or
        # interrupted worker cannot lose intent; the next attempt uses them.
        sources = self.sources()
        await self.audio.update_pins(self.pins())
        with self.accounts.transaction() as connection:
            if startup:
                connection.execute(
                    "UPDATE save_jobs SET state='pending', next_attempt=0 "
                    "WHERE state='running'"
                )
            for source in sources:
                key = f"{source}-{self.policy}"
                entry = self.audio.entries.get(key)
                saved = bool(entry and entry.get("saved"))
                connection.execute(
                    "INSERT OR IGNORE INTO save_jobs(source,policy,state) "
                    "VALUES(?,?,?)",
                    (source, self.policy, "complete" if saved else "pending"),
                )
                if saved:
                    connection.execute(
                        "UPDATE save_jobs SET state='complete',error=NULL "
                        "WHERE source=? AND policy=? AND state!='running'",
                        (source, self.policy),
                    )
                else:
                    connection.execute(
                        "UPDATE save_jobs SET state='pending',next_attempt=0 "
                        "WHERE source=? AND policy=? AND state='complete'",
                        (source, self.policy),
                    )
            rows = connection.execute(
                "SELECT source,policy FROM save_jobs"
            ).fetchall()
            for row in rows:
                if (
                    row["source"] not in sources
                    or row["policy"] != self.policy
                ):
                    connection.execute(
                        "DELETE FROM save_jobs WHERE source=? AND policy=?",
                        tuple(row),
                    )

    async def start(self, audio):
        self.audio = audio
        await self.reconcile(startup=True)
        self.worker = asyncio.create_task(self.run())

    async def close(self):
        if self.worker is not None:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker

    async def run(self):
        recover = False
        while True:
            self.wake.clear()
            try:
                if recover:
                    await self.reconcile(startup=True)
                    recover = False
                await self.process_one()
                with self.accounts.connection() as connection:
                    due = connection.execute(
                        "SELECT MIN(next_attempt) FROM save_jobs "
                        "WHERE state='pending' AND policy=?",
                        (self.policy,),
                    ).fetchone()[0]
            except sqlite3.Error, OSError, CatalogError:
                # If SQLite itself cannot write (for example disk exhaustion),
                # keep durable intents and retry after storage becomes usable.
                logger.warning("Save worker storage unavailable; retrying")
                recover = True
                await asyncio.sleep(5)
                continue
            if due is not None and due <= time.time():
                await asyncio.sleep(0)
                continue
            timeout = max(0.1, due - time.time()) if due is not None else None
            try:
                await asyncio.wait_for(self.wake.wait(), timeout)
            except TimeoutError:
                pass

    async def process_one(self):
        with self.accounts.transaction() as connection:
            job = connection.execute(
                "SELECT * FROM save_jobs WHERE state='pending' AND policy=? "
                "AND next_attempt<=? ORDER BY next_attempt,source LIMIT 1",
                (self.policy, time.time()),
            ).fetchone()
            if job is None:
                return False
            connection.execute(
                "UPDATE save_jobs SET state='running',attempts=attempts+1 "
                "WHERE source=? AND policy=?",
                (job["source"], self.policy),
            )
        key = None
        try:
            key, _ = await self.audio.acquire(job["source"])
            saved = await self.audio.retain(key)
            if saved:
                with self.accounts.transaction() as connection:
                    connection.execute(
                        "UPDATE save_jobs SET state='complete',error=NULL "
                        "WHERE source=? AND policy=? AND state='running'",
                        (job["source"], self.policy),
                    )
        except (CatalogError, OSError) as error:
            # Fixed message only: provider URLs and dependency exception text
            # must not enter the database or another user's browser page.
            attempts = job["attempts"] + 1
            message = "Download failed; check free disk space or retry later."
            reasons = {
                "Not enough free space for audio",
                "Recording exceeds the audio file size limit",
                "Recording unavailable from audio provider",
                "Requested source audio unavailable",
                "Audio provider denied access; try later",
                "Audio provider busy; retry shortly",
                "Audio cache busy; retry shortly",
                "Audio acquisition timed out; retry",
            }
            if isinstance(error, CatalogError) and error.message in reasons:
                message = "Download failed: " + error.message
            with self.accounts.transaction() as connection:
                connection.execute(
                    "UPDATE save_jobs SET state=?,error=?,next_attempt=? "
                    "WHERE source=? AND policy=? AND state='running'",
                    (
                        "failed" if attempts >= 3 else "pending",
                        message,
                        time.time() + (30 if attempts == 1 else 120),
                        job["source"],
                        self.policy,
                    ),
                )
        finally:
            if key is not None:
                await self.audio.release(key)
        return True

    def retry(self, user_id):
        with self.accounts.transaction() as connection:
            connection.execute(
                "UPDATE save_jobs SET state='pending',attempts=0,error=NULL,"
                "next_attempt=0 WHERE state IN ('pending','failed') "
                "AND policy=? "
                "AND source IN (SELECT c.source FROM favorite_tracks t "
                "JOIN catalog c ON c.id=t.song_id WHERE t.user_id=?)",
                (self.policy, user_id),
            )
        self.wake.set()

    def downloads(self, user_id):
        with self.accounts.connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT c.id,j.state,j.attempts,j.error FROM "
                "favorite_tracks t JOIN catalog c ON c.id=t.song_id "
                "JOIN save_jobs j ON j.source=c.source AND j.policy=? "
                "WHERE t.user_id=? ORDER BY c.id",
                (self.policy, user_id),
            ).fetchall()
        return [
            dict(row)
            | {"title": self.catalog.read(row["id"])["document"]["title"]}
            for row in rows
        ]

    def collection(self, user_id, starred_only=False):
        stars = self.stars(user_id)
        with self.accounts.connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT c.id FROM catalog c JOIN favorites f ON "
                "f.item_id=c.id WHERE f.user_id=?",
                (user_id,),
            ).fetchall()
            ids = {row[0] for row in rows}
            if not starred_only:
                ids.update(
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT song_id FROM favorite_tracks "
                        "WHERE user_id=?",
                        (user_id,),
                    )
                )
        items = {item_id: self.catalog.read(item_id) for item_id in ids}
        if not starred_only:
            # Only include album/artist parents of this user's saved songs.
            # Public catalog searches never constitute library membership.
            for item in list(items.values()):
                for field in ("albumId", "artistId"):
                    parent = item["document"].get(field)
                    if parent and parent not in items:
                        items[parent] = self.catalog.read(parent)
            for item in list(items.values()):
                parent = item["document"].get("artistId")
                if parent and parent not in items:
                    items[parent] = self.catalog.read(parent)
        result = {"artist": [], "album": [], "song": []}
        song_docs = [
            r["document"] for r in items.values() if r["kind"] == "song"
        ]
        for item_id, item in items.items():
            kind = item["kind"]
            doc = {
                k: v
                for k, v in item["document"].items()
                if k not in ("starred", "song")
                and not (k == "album" and isinstance(v, list))
            }
            if item_id in stars:
                doc["starred"] = stars[item_id]
            if not starred_only and kind == "album":
                songs = [s for s in song_docs if s.get("albumId") == item_id]
                doc["songCount"] = len(songs)
                doc["duration"] = sum(s.get("duration", 0) for s in songs)
            if not starred_only and kind == "artist":
                doc["albumCount"] = sum(
                    r["kind"] == "album"
                    and r["document"].get("artistId") == item_id
                    for r in items.values()
                )
            result[kind].append(doc)
        for kind in result:
            result[kind].sort(
                key=lambda d: (
                    d.get("name", d.get("title", "")).casefold(),
                    d["id"],
                )
            )
        return result
