import time


class DeliveryRepository:
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
        return dict(user)

    def claim_delivery(self):
        with self.database.transaction() as connection:
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
                return None
            user = self.current_user(connection, job["user_id"], job["target"])
            if user is None:
                connection.execute(
                    "UPDATE scrobbles SET state='blocked',error="
                    "'Relink the original Last.fm account "
                    "to send this report.' "
                    "WHERE id=?",
                    (job["id"],),
                )
                return dict(job), None
            connection.execute(
                "UPDATE scrobbles SET state='running',attempts=attempts+1 "
                "WHERE id=?",
                (job["id"],),
            )
        return dict(job), dict(user)

    def invalidate(self, user):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE users SET link_status='relink',lastfm_secret=NULL "
                "WHERE id=? AND lastfm_secret=?",
                (user["id"], user["lastfm_secret"]),
            )

    def complete_delivery(self, job, state, error):
        with self.database.transaction() as connection:
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
