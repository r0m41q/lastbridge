import json
import time

from lastbridge.features.radio.domain.models import Exclusions


class RadioRepository:
    def __init__(self, database):
        self.database = database

    def snapshot(self, user):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM radio WHERE user_id=?", (user["id"],)
            ).fetchone()
        if row and row["target"] == (user["lastfm_name"] or "").casefold():
            return dict(row)
        return None

    def publish(self, user, seed, songs):
        target = (user["lastfm_name"] or "").casefold()
        now = time.time()
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT lastfm_name FROM users WHERE id=?", (user["id"],)
            ).fetchone()
            if not current or (current[0] or "").casefold() != target:
                return
            connection.execute(
                "INSERT INTO radio VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET target=excluded.target,"
                "seed_id=excluded.seed_id,songs=excluded.songs,"
                "updated=excluded.updated",
                (
                    user["id"],
                    target,
                    seed["id"],
                    json.dumps([s["id"] for s in songs]),
                    now,
                    now,
                ),
            )

    def exclusions(self, user):
        with self.database.connection() as connection:
            recent_ids = set()
            ids = set()
            # Include starts without treating them as completed listening.
            for row in connection.execute(
                "SELECT song_id FROM scrobbles "
                "WHERE user_id=? AND received>=? "
                "UNION SELECT song_id FROM playback_starts "
                "WHERE user_id=? AND timestamp>=?",
                (
                    user["id"],
                    time.time() - 86400,
                    user["id"],
                    time.time() - 86400,
                ),
            ):
                ids.add(row[0])
                recent_ids.add(row[0])
            removed = {
                r[0]
                for r in connection.execute(
                    "SELECT recording_key FROM love_removals WHERE user_id=?",
                    (user["id"],),
                )
            }
            ids |= {
                r[0]
                for r in connection.execute(
                    "SELECT item_id FROM favorite_removals WHERE user_id=?",
                    (user["id"],),
                )
            }
        return Exclusions(recent_ids, removed, ids)

    def artist_song(self, item_id):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT id FROM catalog WHERE kind='song' "
                "AND json_extract(document,'$.artistId')=? "
                "ORDER BY complete DESC,updated DESC,id LIMIT 1",
                (item_id,),
            ).fetchone()
            return row[0] if row else None
