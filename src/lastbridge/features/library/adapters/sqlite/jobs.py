import time


class JobRepository:
    def reconcile(self, sources, saved_keys, startup=False):
        with self.database.transaction() as connection:
            if startup:
                connection.execute(
                    "UPDATE save_jobs SET state='pending', next_attempt=0 "
                    "WHERE state='running'"
                )
            for source in sources:
                key = f"{source}-{self.policy}"
                saved = key in saved_keys
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

    def claim(self):
        with self.database.transaction() as connection:
            job = connection.execute(
                "SELECT * FROM save_jobs WHERE state='pending' AND policy=? "
                "AND next_attempt<=? ORDER BY next_attempt,source LIMIT 1",
                (self.policy, time.time()),
            ).fetchone()
            if job is None:
                return None
            connection.execute(
                "UPDATE save_jobs SET state='running',attempts=attempts+1 "
                "WHERE source=? AND policy=?",
                (job["source"], self.policy),
            )
        return dict(job)

    def complete(self, source):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE save_jobs SET state='complete',error=NULL "
                "WHERE source=? AND policy=? AND state='running'",
                (source, self.policy),
            )

    def failed(self, source, attempts, message):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE save_jobs SET state=?,error=?,next_attempt=? "
                "WHERE source=? AND policy=? AND state='running'",
                (
                    "failed" if attempts >= 3 else "pending",
                    message,
                    time.time() + (30 if attempts == 1 else 120),
                    source,
                    self.policy,
                ),
            )

    def next_due(self):
        with self.database.connection() as connection:
            return connection.execute(
                "SELECT MIN(next_attempt) FROM save_jobs "
                "WHERE state='pending' AND policy=?",
                (self.policy,),
            ).fetchone()[0]

    def retry(self, user_id):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE save_jobs SET state='pending',attempts=0,error=NULL,"
                "next_attempt=0 WHERE state IN ('pending','failed') "
                "AND policy=? "
                "AND source IN (SELECT c.source FROM retained_tracks t "
                "JOIN catalog c ON c.id=t.song_id WHERE t.user_id=?)",
                (self.policy, user_id),
            )

    def downloads(self, user_id):
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT c.id,j.state,j.attempts,j.error FROM "
                "retained_tracks t JOIN catalog c ON c.id=t.song_id "
                "JOIN save_jobs j ON j.source=c.source AND j.policy=? "
                "WHERE t.user_id=? ORDER BY c.id",
                (self.policy, user_id),
            ).fetchall()
        return [dict(row) for row in rows]
