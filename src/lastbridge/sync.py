import asyncio
import contextlib
import logging
import sqlite3
import time

from lastbridge.catalog import CatalogError
from lastbridge.lastfm import LastfmRequestError
from lastbridge.providers.base import ProviderError
from lastbridge.recordings import recording_key

logger = logging.getLogger(__name__)


class LastfmSync:
    def __init__(self, accounts, catalog, library, lastfm, client):
        self.accounts, self.catalog, self.library = accounts, catalog, library
        self.lastfm, self.client = lastfm, client
        self.tasks = []
        self.wake = asyncio.Event()

    def linked(self, user_id):
        """Create intent without waiting for provider reads."""
        with self.accounts.transaction() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if (
                not user
                or not user["lastfm_name"]
                or not user["lastfm_secret"]
            ):
                return
            target = user["lastfm_name"].casefold()
            connection.execute(
                "INSERT INTO love_imports(user_id,target) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET target=excluded.target,"
                "state='active',next_page=1,total_pages=NULL,fetched=0,"
                "next_attempt=0,error=NULL,revision=revision+1 "
                "WHERE target!=excluded.target",
                (user_id, target),
            )
            connection.execute(
                "UPDATE scrobbles SET state='pending',"
                "next_attempt=0,error=NULL "
                "WHERE user_id=? AND target=? AND state='blocked'",
                (user_id, target),
            )
        self.wake.set()

    def report(self, user, values):
        submission = values.get("submission", "true")
        if submission not in ("true", "false"):
            raise CatalogError(10, "Invalid parameter: submission")
        ids, times = values.get("id", []), values.get("time", [])
        if not ids or any(not item for item in ids):
            raise CatalogError(10, "Supply at least one song id")
        if times and len(times) != len(ids):
            raise CatalogError(10, "Supply one time for each song id")
        now = int(time.time())
        prepared = []
        for index, item_id in enumerate(ids):
            song = self.catalog.read(item_id, "song")["document"]
            try:
                stamp = int(times[index]) // 1000 if times else now
                if stamp <= 0 or stamp > now + 300:
                    raise ValueError
            except ValueError:
                raise CatalogError(10, "Invalid parameter: time") from None
            prepared.append((song, stamp))
        if submission == "false":
            # Keep a local start marker for clients such as Feishin which omit
            # time on submissions. This is never sent as a Last.fm now-playing.
            with self.accounts.transaction() as connection:
                connection.execute(
                    "DELETE FROM playback_starts WHERE timestamp<?",
                    (now - 86400,),
                )
                for song, stamp in prepared:
                    connection.execute(
                        "INSERT INTO playback_starts VALUES(?,?,?,?) "
                        "ON CONFLICT(user_id,song_id,client) DO UPDATE SET "
                        "timestamp=excluded.timestamp WHERE timestamp<?",
                        (
                            user["id"],
                            song["id"],
                            values.get("c", ""),
                            stamp,
                            now - 30,
                        ),
                    )
            return
        target = (user["lastfm_name"] or "").casefold()
        if not target:
            raise CatalogError(50, "Link Last.fm before submitting playback")
        with self.accounts.transaction() as connection:
            for song, stamp in prepared:
                if not times:
                    started = connection.execute(
                        "SELECT timestamp FROM playback_starts "
                        "WHERE user_id=? "
                        "AND song_id=? AND client=? AND timestamp>=?",
                        (
                            user["id"],
                            song["id"],
                            values.get("c", ""),
                            now - max(3600, song.get("duration", 0) + 3600),
                        ),
                    ).fetchone()
                    if started:
                        stamp = started["timestamp"]
                # Clients omitting start time cannot provide an exact identity.
                # Bound retries, accepting this documented tradeoff.
                if (
                    not times
                    and connection.execute(
                        "SELECT 1 FROM scrobbles WHERE user_id=? AND target=? "
                        "AND song_id=? AND received>=?",
                        (user["id"], target, song["id"], now - 30),
                    ).fetchone()
                ):
                    continue
                duration = song.get("duration", 0)
                artist, title = song.get("artist", ""), song["title"]
                state, error = "pending", None
                if duration <= 30:
                    state, error = (
                        "ignored",
                        "Track is not longer than 30 seconds.",
                    )
                elif not artist:
                    state, error = "failed", "Track artist is missing."
                connection.execute(
                    "INSERT OR IGNORE INTO scrobbles(user_id,target,song_id,"
                    "timestamp,received,artist,title,album,"
                    "duration,state,error) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        user["id"],
                        target,
                        song["id"],
                        stamp,
                        now,
                        artist,
                        title,
                        song.get("album", ""),
                        duration,
                        state,
                        error,
                    ),
                )
        self.wake.set()

    async def start(self):
        with self.accounts.transaction() as connection:
            # A crash/cancellation after dispatch cannot prove remote failure.
            connection.execute(
                "UPDATE scrobbles SET state='uncertain',error="
                "'Interrupted delivery; review before retrying.' "
                "WHERE state='running'"
            )
            users = connection.execute(
                "SELECT id FROM users WHERE link_status='linked' "
                "AND lastfm_secret IS NOT NULL"
            ).fetchall()
        for user in users:
            self.linked(user["id"])
        self.tasks = [
            asyncio.create_task(self.loop(self.deliver_one)),
            asyncio.create_task(self.loop(self.import_one)),
        ]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.tasks = []

    async def loop(self, operation):
        recover = False
        while True:
            self.wake.clear()
            try:
                if self.lastfm.configured:
                    if recover and operation == self.deliver_one:
                        with self.accounts.transaction() as connection:
                            connection.execute(
                                "UPDATE scrobbles SET state='uncertain',error="
                                "'Storage interrupted delivery; review before "
                                "retrying.' WHERE state='running'"
                            )
                    recover = False
                    await operation()
            except sqlite3.Error, OSError:
                logger.warning("Last.fm sync storage unavailable; retrying")
                recover = True
            # Idle polling never blocks playback. Imports additionally persist
            # a two-second gap between operations for each account.
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1)
            except TimeoutError:
                pass

    def current_user(self, connection, user_id, target):
        user = connection.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if (
            user is None
            or user["link_status"] != "linked"
            or (user["lastfm_name"] or "").casefold() != target
            or not user["lastfm_secret"]
        ):
            return None
        return user

    async def deliver_one(self):
        with self.accounts.transaction() as connection:
            # Preserve ordering within each account while other users advance.
            job = connection.execute(
                "SELECT s.* FROM scrobbles s WHERE state='pending' "
                "AND next_attempt<=? AND NOT EXISTS "
                "(SELECT 1 FROM scrobbles p "
                "WHERE p.user_id=s.user_id AND p.target=s.target "
                "AND p.state IN ('pending','running') "
                "AND (p.timestamp<s.timestamp OR "
                "(p.timestamp=s.timestamp AND p.id<s.id))) "
                "ORDER BY next_attempt,timestamp,id LIMIT 1",
                (time.time(),),
            ).fetchone()
            if job is None:
                return False
            user = self.current_user(connection, job["user_id"], job["target"])
            if user is None:
                connection.execute(
                    "UPDATE scrobbles SET state='blocked',error="
                    "'Relink the original Last.fm account "
                    "to send this report.' "
                    "WHERE id=?",
                    (job["id"],),
                )
                return True
            connection.execute(
                "UPDATE scrobbles SET state='running',attempts=attempts+1 "
                "WHERE id=?",
                (job["id"],),
            )
        try:
            state, error = await self.lastfm.scrobble(
                self.client, self.accounts.decrypt(user["lastfm_secret"]), job
            )
        except LastfmRequestError as failure:
            state, error = failure.state, str(failure)
            if failure.code == 9:
                # A concurrent successful relink must not be invalidated by an
                # error from the old credential that was already in flight.
                with self.accounts.transaction() as connection:
                    connection.execute(
                        "UPDATE users SET link_status='relink',"
                        "lastfm_secret=NULL "
                        "WHERE id=? AND lastfm_secret=?",
                        (user["id"], user["lastfm_secret"]),
                    )
        with self.accounts.transaction() as connection:
            connection.execute(
                "UPDATE scrobbles SET state=?,error=?,next_attempt=? "
                "WHERE id=? AND state='running'",
                (
                    state,
                    error,
                    time.time() + min(3600, 30 * 2 ** min(job["attempts"], 7)),
                    job["id"],
                ),
            )
        return True

    def control(self, user_id, action):
        with self.accounts.transaction() as connection:
            if action in ("pause", "resume", "refresh", "retry-matches"):
                connection.execute(
                    "UPDATE love_imports SET revision=revision+1 "
                    "WHERE user_id=?",
                    (user_id,),
                )
                if action == "pause":
                    connection.execute(
                        "UPDATE love_imports SET state='paused' "
                        "WHERE user_id=?",
                        (user_id,),
                    )
                else:
                    if action == "refresh":
                        connection.execute(
                            "UPDATE love_imports SET next_page=1,"
                            "total_pages=NULL,"
                            "fetched=0 WHERE user_id=?",
                            (user_id,),
                        )
                    if action == "retry-matches":
                        connection.execute(
                            "UPDATE loved_items SET state='pending',"
                            "attempts=0,"
                            "next_attempt=0,error=NULL WHERE user_id=? "
                            "AND state='unmatched' AND target=(SELECT target "
                            "FROM love_imports WHERE user_id=?)",
                            (user_id, user_id),
                        )
                    connection.execute(
                        "UPDATE love_imports SET state='active',"
                        "next_attempt=0,"
                        "error=NULL WHERE user_id=?",
                        (user_id,),
                    )
            elif action in ("retry-scrobbles", "retry-uncertain"):
                user = connection.execute(
                    "SELECT lastfm_name FROM users WHERE id=?", (user_id,)
                ).fetchone()
                target = (user["lastfm_name"] or "").casefold() if user else ""
                states = (
                    ("uncertain",)
                    if action == "retry-uncertain"
                    else ("failed", "blocked", "pending")
                )
                for state in states:
                    connection.execute(
                        "UPDATE scrobbles SET state='pending',next_attempt=0,"
                        "error=NULL WHERE user_id=? AND state=? AND target=?",
                        (user_id, state, target),
                    )
            else:
                raise ValueError("Unknown sync action")
        self.wake.set()

    def snapshot(self, user_id):
        with self.accounts.connection() as connection:
            job = connection.execute(
                "SELECT * FROM love_imports WHERE user_id=?", (user_id,)
            ).fetchone()
            counts, items = {}, []
            if job:
                counts = dict(
                    connection.execute(
                        "SELECT state,count(*) FROM loved_items "
                        "WHERE user_id=? "
                        "AND target=? GROUP BY state",
                        (user_id, job["target"]),
                    ).fetchall()
                )
                items = [
                    dict(r)
                    for r in connection.execute(
                        "SELECT artist,title,state,error FROM loved_items "
                        "WHERE user_id=? AND target=? AND state IN "
                        "('unmatched','skipped') "
                        "ORDER BY artist,title LIMIT 200",
                        (user_id, job["target"]),
                    )
                ]
            reports = [
                dict(r)
                for r in connection.execute(
                    "SELECT id,target,title,timestamp,state,error,attempts "
                    "FROM scrobbles WHERE user_id=? "
                    "ORDER BY id DESC LIMIT 100",
                    (user_id,),
                )
            ]
        return dict(job) if job else None, counts, items, reports

    async def import_one(self):
        now = time.time()
        with self.accounts.transaction() as connection:
            job = connection.execute(
                "SELECT i.* FROM love_imports i "
                "JOIN users u ON u.id=i.user_id "
                "WHERE i.state='active' AND i.next_attempt<=? "
                "AND u.link_status='linked' AND u.lastfm_secret IS NOT NULL "
                "ORDER BY i.next_attempt,i.user_id LIMIT 1",
                (now,),
            ).fetchone()
            if job is None:
                return False
            user_id, target = job["user_id"], job["target"]
            if self.current_user(connection, user_id, target) is None:
                return False
            # Persist pacing before await and advance other users fairly.
            connection.execute(
                "UPDATE love_imports SET next_attempt=? WHERE user_id=?",
                (now + 2, user_id),
            )
            item = connection.execute(
                "SELECT * FROM loved_items WHERE user_id=? AND target=? "
                "AND state='pending' AND next_attempt<=? "
                "ORDER BY next_attempt,recording_key LIMIT 1",
                (user_id, target, now),
            ).fetchone()
        try:
            if item:
                await self.resolve(job, item)
            elif (
                job["total_pages"] is None
                or job["next_page"] <= job["total_pages"]
            ):
                rows, total = await self.lastfm.loved(
                    self.client, target, job["next_page"]
                )
                with self.accounts.transaction() as connection:
                    # Pause/relink during a read must not advance its cursor.
                    if not self.active(connection, job):
                        return True
                    for artist, title in rows:
                        key = recording_key(artist, title)
                        removed = connection.execute(
                            "SELECT 1 FROM love_removals WHERE user_id=? "
                            "AND recording_key=?",
                            (user_id, key),
                        ).fetchone()
                        connection.execute(
                            "INSERT OR IGNORE INTO loved_items(user_id,target,"
                            "recording_key,artist,title,state) "
                            "VALUES(?,?,?,?,?,?)",
                            (
                                user_id,
                                target,
                                key,
                                artist,
                                title,
                                "skipped" if removed else "pending",
                            ),
                        )
                    connection.execute(
                        "UPDATE love_imports SET next_page=?,total_pages=?,"
                        "fetched=fetched+?,error=NULL WHERE user_id=?",
                        (job["next_page"] + 1, total, len(rows), user_id),
                    )
            else:
                with self.accounts.transaction() as connection:
                    if not self.active(connection, job):
                        return True
                    pending = connection.execute(
                        "SELECT 1 FROM loved_items "
                        "WHERE user_id=? AND target=? "
                        "AND state='pending' LIMIT 1",
                        (user_id, target),
                    ).fetchone()
                    if not pending:
                        connection.execute(
                            "UPDATE love_imports SET state='complete',"
                            "error=NULL "
                            "WHERE user_id=?",
                            (user_id,),
                        )
        except LastfmRequestError as error:
            with self.accounts.transaction() as connection:
                if self.active(connection, job):
                    connection.execute(
                        "UPDATE love_imports SET next_attempt=?,"
                        "error=?,state=? "
                        "WHERE user_id=?",
                        (
                            now + 60,
                            str(error),
                            "paused"
                            if error.state in ("failed", "blocked")
                            else "active",
                            user_id,
                        ),
                    )
        return True

    def active(self, connection, job):
        return bool(
            connection.execute(
                "SELECT 1 FROM love_imports WHERE user_id=? AND target=? "
                "AND state='active' AND next_page=? AND revision=?",
                (
                    job["user_id"],
                    job["target"],
                    job["next_page"],
                    job["revision"],
                ),
            ).fetchone()
            and self.current_user(connection, job["user_id"], job["target"])
        )

    async def resolve(self, job, item):
        user_id, target, key = (
            item["user_id"],
            item["target"],
            item["recording_key"],
        )
        state, error, song_id = "pending", None, None
        try:
            with self.accounts.connection() as connection:
                removed = connection.execute(
                    "SELECT 1 FROM love_removals "
                    "WHERE user_id=? AND recording_key=?",
                    (user_id, key),
                ).fetchone()
            if removed:
                state, error = "skipped", "Removed locally."
            else:
                song = await self.catalog.match_loved(
                    item["artist"], item["title"]
                )
                with self.accounts.connection() as connection:
                    if not self.active(connection, job):
                        return
                saved = await self.library.change(
                    user_id,
                    [(song["id"], "song")],
                    True,
                    imported=(target, key),
                )
                state = "matched" if saved else "skipped"
                song_id = song["id"]
                error = None if saved else "Removed locally."
        except ProviderError as failure:
            state = "unmatched"
            error = (
                "Multiple recordings match."
                if str(failure) == ("ambiguous_match")
                else "No confident recording match."
            )
        except CatalogError, OSError:
            state = "unmatched" if item["attempts"] >= 2 else "pending"
            error = "Recording lookup or storage unavailable; retry later."
        with self.accounts.transaction() as connection:
            if self.active(connection, job):
                connection.execute(
                    "UPDATE loved_items SET state=?,song_id=?,error=?,"
                    "attempts=attempts+1,next_attempt=? WHERE user_id=? "
                    "AND target=? AND recording_key=? AND state='pending'",
                    (
                        state,
                        song_id,
                        error,
                        time.time() + 60,
                        user_id,
                        target,
                        key,
                    ),
                )
