import time

from lastbridge.shared.recordings import recording_key


class LoveIntentWriter:
    def record(self, connection, user_id, stored, enabled):
        user = connection.execute(
            "SELECT lastfm_name,link_status FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not user or not user["lastfm_name"]:
            return
        target = user["lastfm_name"].casefold()
        # Retain Last.fm's original credit/title when unliking an import.
        original = connection.execute(
            "SELECT artist,title FROM loved_items WHERE user_id=? "
            "AND target=? AND song_id=? ORDER BY recording_key LIMIT 1",
            (user_id, target, stored["id"]),
        ).fetchone()
        document = dict(original) if original else stored["document"]
        artist, title = document.get("artist", ""), document.get("title", "")
        if not artist.strip() or not title.strip():
            return
        state = "pending" if user["link_status"] == "linked" else "blocked"
        connection.execute(
            "INSERT INTO love_outbox(user_id,target,recording_key,"
            "artist,title,enabled,state,updated) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id,target,recording_key) DO UPDATE SET "
            "artist=excluded.artist,title=excluded.title,"
            "enabled=excluded.enabled,revision=love_outbox.revision+1,"
            "updated=excluded.updated,"
            "state=CASE WHEN love_outbox.state='running' THEN 'running' "
            "ELSE excluded.state END,attempts=0,next_attempt=0,error=NULL "
            "WHERE love_outbox.enabled!=excluded.enabled "
            "OR love_outbox.state IN ('failed','blocked')",
            (
                user_id,
                target,
                recording_key(artist, title),
                artist,
                title,
                int(enabled),
                state,
                time.time(),
            ),
        )


class LoveRepository:
    def claim_love(self):
        with self.database.transaction() as connection:
            job = connection.execute(
                "SELECT * FROM love_outbox WHERE state='pending' "
                "AND next_attempt<=? ORDER BY next_attempt,id LIMIT 1",
                (time.time(),),
            ).fetchone()
            if job is None:
                return None
            user = self.current_user(connection, job["user_id"], job["target"])
            if user is None:
                connection.execute(
                    "UPDATE love_outbox SET state='blocked',error="
                    "'Relink the original Last.fm account "
                    "to send this change.' "
                    "WHERE id=?",
                    (job["id"],),
                )
                return dict(job), None
            connection.execute(
                "UPDATE love_outbox SET state='running',attempts=attempts+1 "
                "WHERE id=?",
                (job["id"],),
            )
        return dict(job), user

    def complete_love(self, job, state, error):
        with self.database.transaction() as connection:
            # A newer local edit must survive completion of an older request.
            connection.execute(
                "UPDATE love_outbox SET "
                "state=CASE WHEN revision=? THEN ? ELSE 'pending' END,"
                "error=CASE WHEN revision=? THEN ? ELSE NULL END,"
                "next_attempt=CASE WHEN revision=? THEN ? ELSE 0 END "
                "WHERE id=? AND state='running'",
                (
                    job["revision"],
                    state,
                    job["revision"],
                    error,
                    job["revision"],
                    time.time() + min(3600, 30 * 2 ** min(job["attempts"], 7)),
                    job["id"],
                ),
            )

    def recover_loves(self):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE love_outbox SET state='pending',next_attempt=0 "
                "WHERE state='running'"
            )

    def love_reports(self, user_id):
        with self.database.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT target,artist,title,enabled,state,attempts,error "
                    "FROM love_outbox WHERE user_id=? "
                    "ORDER BY updated DESC,id DESC LIMIT 100",
                    (user_id,),
                )
            ]
