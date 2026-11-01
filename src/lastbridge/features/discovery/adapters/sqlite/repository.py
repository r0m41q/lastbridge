import json
import time

from lastbridge.features.discovery.domain.models import TasteSample
from lastbridge.features.discovery.domain.policy import COOLDOWN, key


class MixRepository:
    def __init__(self, database):
        self.database = database

    def linked(self, user_id):
        with self.database.transaction() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if user is None or not user["lastfm_name"]:
                return
            previous = connection.execute(
                "SELECT target FROM mixes WHERE user_id=?", (user_id,)
            ).fetchone()
            if not previous or previous[0] != user["lastfm_name"].casefold():
                connection.execute(
                    "INSERT INTO mix_activity VALUES(?,?) "
                    "ON CONFLICT(user_id) "
                    "DO UPDATE SET last_active=excluded.last_active",
                    (user_id, time.time()),
                )
            connection.execute(
                "INSERT INTO mixes(user_id,target,created) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET target=excluded.target,"
                "revision=revision+1,running=0,next_attempt=0,requested=0,"
                "updated=0,error=NULL,profile='{}',snapshot='{}' "
                "WHERE target!=excluded.target",
                (user_id, user["lastfm_name"].casefold(), time.time()),
            )
        return True

    def touch(self, user_id):
        """Persist meaningful use at most once a minute; wake overdue work."""
        now = time.time()
        with self.database.transaction() as connection:
            changed = connection.execute(
                "INSERT INTO mix_activity SELECT user_id,? FROM mixes "
                "WHERE user_id=? ON CONFLICT(user_id) DO UPDATE "
                "SET last_active=excluded.last_active "
                "WHERE last_active<=?",
                (now, user_id, now - 60),
            ).rowcount
        return bool(changed)

    def request(self, user_id):
        now = time.time()
        with self.database.transaction() as connection:
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

    def status(self, user_id):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT m.*,COALESCE(a.last_active,0) AS last_active "
                "FROM mixes m LEFT JOIN mix_activity a ON a.user_id=m.user_id "
                "WHERE m.user_id=?",
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
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def local(self, user_id, target):
        with self.database.connection() as connection:
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
        return TasteSample(saved, recent, loved, known, removed, removed_ids)

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

    def startup(self, refresh_interval):
        with self.database.transaction() as connection:
            # Adopt interval changes without postponing queued manual work or
            # changing an existing outage retry deadline.
            connection.execute(
                "UPDATE mixes SET next_attempt=updated+? "
                "WHERE updated>0 AND running=0 AND error IS NULL "
                "AND requested<=updated AND next_attempt>0",
                (refresh_interval,),
            )
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
        return ids

    def claim(self, inactive_ttl):
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT m.* FROM mixes m JOIN users u ON u.id=m.user_id "
                "JOIN mix_activity a ON a.user_id=m.user_id "
                "WHERE m.running=0 AND m.next_attempt<=? AND "
                "a.last_active>? AND "
                "u.link_status='linked' "
                "AND u.lastfm_secret IS NOT NULL ORDER BY "
                "m.next_attempt,m.user_id LIMIT 1",
                (time.time(), time.time() - inactive_ttl),
            ).fetchone()
            if row is None:
                return None
            job = dict(row)
            if not self.active(connection, job):
                # A changed link is initialized on callback or next startup.
                connection.execute(
                    "UPDATE mixes SET next_attempt=? WHERE user_id=?",
                    (time.time() + COOLDOWN, job["user_id"]),
                )
                return {}
            connection.execute(
                "UPDATE mixes SET running=1,error=NULL WHERE user_id=?",
                (job["user_id"],),
            )
        return dict(job)

    def failed(self, job):
        with self.database.transaction() as connection:
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

    def publish(self, job, profile, snapshot, refresh_interval):
        with self.database.transaction() as connection:
            if self.active(connection, job):
                connection.execute(
                    "UPDATE mixes SET "
                    "running=0,next_attempt=?,updated=?,error=NULL,"
                    "profile=?,snapshot=? WHERE user_id=?",
                    (
                        time.time() + refresh_interval,
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

    def recover(self):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE mixes SET running=0,next_attempt=? WHERE running=1",
                (time.time() + COOLDOWN,),
            )
