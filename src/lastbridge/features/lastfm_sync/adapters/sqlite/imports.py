import time

from lastbridge.shared.recordings import recording_key


class ImportRepository:
    def claim_import(self):
        now = time.time()
        with self.database.transaction() as connection:
            job = connection.execute(
                "SELECT i.* FROM love_imports i "
                "JOIN users u ON u.id=i.user_id "
                "WHERE i.state='active' AND i.next_attempt<=? "
                "AND u.link_status='linked' AND u.lastfm_secret IS NOT NULL "
                "ORDER BY i.next_attempt,i.user_id LIMIT 1",
                (now,),
            ).fetchone()
            if job is None:
                return None
            user_id, target = job["user_id"], job["target"]
            if self.current_user(connection, user_id, target) is None:
                return None
            # Persist pacing before await and advance other users fairly.
            connection.execute(
                "UPDATE love_imports SET next_attempt=? WHERE user_id=?",
                (now + 2, user_id),
            )
            item = connection.execute(
                "SELECT * FROM loved_items WHERE user_id=? AND target=? "
                "AND state='pending' AND review_skipped=0 AND next_attempt<=? "
                "ORDER BY next_attempt,recording_key LIMIT 1",
                (user_id, target, now),
            ).fetchone()
        return dict(job), dict(item) if item else None

    def save_page(self, job, rows, total):
        user_id, target = job["user_id"], job["target"]
        with self.database.transaction() as connection:
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

    def finish_import(self, job):
        user_id, target = job["user_id"], job["target"]
        with self.database.transaction() as connection:
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

    def fail_import(self, job, now, error):
        user_id = job["user_id"]
        with self.database.transaction() as connection:
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

    def resolved(self, job, item, state, error, song_id):
        user_id, target, key = (
            item["user_id"],
            item["target"],
            item["recording_key"],
        )
        with self.database.transaction() as connection:
            if self.active(connection, job):
                connection.execute(
                    "UPDATE loved_items SET state=?,song_id=?,error=?,"
                    "attempts=attempts+1,next_attempt=? WHERE user_id=? "
                    "AND target=? AND recording_key=? AND state='pending' "
                    "AND manual_song_id IS NULL AND review_skipped=0",
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

    def removed(self, user_id, key):
        with self.database.connection() as connection:
            return bool(
                connection.execute(
                    "SELECT 1 FROM love_removals WHERE user_id=? AND "
                    "recording_key=?",
                    (user_id, key),
                ).fetchone()
            )

    def is_active(self, job):
        with self.database.connection() as connection:
            return self.active(connection, job)
