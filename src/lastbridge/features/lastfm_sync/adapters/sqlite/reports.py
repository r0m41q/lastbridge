from lastbridge.features.lastfm_sync.domain.reporting import initial_state
from lastbridge.shared.errors import CatalogError


class ReportRepository:
    def linked(self, user_id):
        """Create intent without waiting for provider reads."""
        with self.database.transaction() as connection:
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
            connection.execute(
                "UPDATE love_outbox SET state='pending',"
                "next_attempt=0,error=NULL "
                "WHERE user_id=? AND target=? AND state='blocked'",
                (user_id, target),
            )
        return True

    def record(self, user, values, prepared, now):
        submission = values.get("submission", "true")
        times = values.get("time", [])
        if submission == "false":
            # Keep a local start marker for clients such as Feishin which omit
            # time on submissions. This is never sent as a Last.fm now-playing.
            with self.database.transaction() as connection:
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
        with self.database.transaction() as connection:
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
                state, error = initial_state(duration, artist)
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

    def control(self, user_id, action):
        with self.database.transaction() as connection:
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
            elif action == "retry-loves":
                user = connection.execute(
                    "SELECT lastfm_name FROM users WHERE id=?", (user_id,)
                ).fetchone()
                target = (user["lastfm_name"] or "").casefold() if user else ""
                connection.execute(
                    "UPDATE love_outbox SET state='pending',next_attempt=0,"
                    "error=NULL WHERE user_id=? AND state IN "
                    "('failed','blocked','pending') AND target=?",
                    (user_id, target),
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

    def snapshot(self, user_id):
        with self.database.connection() as connection:
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
                        "SELECT recording_key,artist,title,state,error,"
                        "review_skipped "
                        "FROM loved_items "
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

    def startup(self):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE love_outbox SET state='pending',next_attempt=0 "
                "WHERE state='running'"
            )
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
        return [dict(user) for user in users]

    def recover_delivery(self):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE scrobbles SET state='uncertain',error="
                "'Storage interrupted delivery; review before "
                "retrying.' WHERE state='running'"
            )
