"""Administrator read model. Writes stay with each feature's service."""


class DashboardRepository:
    def __init__(self, database, policy):
        self.database, self.policy = database, policy

    def user(self, user_id):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT id,username,link_status FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def snapshot(self):
        with self.database.connection() as connection:
            # One read transaction keeps account/job/ownership rows consistent.
            connection.execute("BEGIN")
            users = [
                dict(r)
                for r in connection.execute(
                    "SELECT id,username,admin,link_status,lastfm_name "
                    "FROM users "
                    "ORDER BY username"
                )
            ]
            retained = {}
            for row in connection.execute(
                "SELECT DISTINCT t.user_id,c.source FROM retained_tracks t "
                "JOIN catalog c ON c.id=t.song_id"
            ):
                retained.setdefault(row["user_id"], set()).add(row["source"])
            job_counts = dict(
                connection.execute(
                    "SELECT state,count(*) FROM save_jobs WHERE policy=? "
                    "GROUP BY state",
                    (self.policy,),
                )
            )
            jobs = [
                dict(r)
                for r in connection.execute(
                    "SELECT j.source,j.state,j.attempts,"
                    "MAX(json_extract(c.document,'$.title')) AS title,"
                    "count(DISTINCT t.user_id) "
                    "AS owners FROM save_jobs j LEFT JOIN catalog c ON "
                    "c.source=j.source AND c.kind='song' LEFT JOIN "
                    "retained_tracks t ON t.song_id=c.id WHERE j.policy=? "
                    "AND j.state='failed' "
                    "GROUP BY j.source,j.state,j.attempts "
                    "ORDER BY j.source LIMIT 100",
                    (self.policy,),
                )
            ]
            for user in users:
                user_id = user["id"]
                user["downloads"] = dict(
                    connection.execute(
                        "SELECT j.state,count(DISTINCT j.source) "
                        "FROM save_jobs j "
                        "JOIN catalog c ON c.source=j.source "
                        "JOIN retained_tracks "
                        "t ON t.song_id=c.id WHERE t.user_id=? AND j.policy=? "
                        "GROUP BY j.state",
                        (user_id, self.policy),
                    )
                )
                mix = connection.execute(
                    "SELECT running,error,updated FROM mixes WHERE user_id=?",
                    (user_id,),
                ).fetchone()
                user["mix"] = dict(mix) if mix else None
                job = connection.execute(
                    "SELECT state,fetched,total_pages,next_page,error "
                    "FROM love_imports WHERE user_id=?",
                    (user_id,),
                ).fetchone()
                user["import"] = dict(job) if job else None
                user["scrobbles"] = dict(
                    connection.execute(
                        "SELECT state,count(*) FROM scrobbles WHERE user_id=? "
                        "GROUP BY state",
                        (user_id,),
                    )
                )
            charts = [
                dict(r)
                for r in connection.execute(
                    "SELECT id,title,updated,next_attempt,token IS NOT NULL "
                    "AS running,error FROM playlist_imports "
                    "WHERE shared=1 AND ready=1 ORDER BY title,id"
                )
            ]
        return {
            "users": users,
            "retained": retained,
            "jobs": jobs,
            "job_counts": job_counts,
            "charts": charts,
        }
