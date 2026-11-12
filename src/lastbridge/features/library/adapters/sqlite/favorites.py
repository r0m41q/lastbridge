import time

from lastbridge.features.library.adapters.sqlite.loves import LoveIntentWriter
from lastbridge.shared.recordings import recording_key


class FavoriteRepository:
    def __init__(
        self, database, policy, love_intents: LoveIntentWriter | None = None
    ):
        self.database, self.policy = database, policy
        self.love_intents = love_intents

    def sources(self):
        with self.database.connection() as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT c.source FROM retained_audio t "
                    "JOIN catalog c ON c.id=t.recording_id"
                )
            }

    def stars(self, user_id):
        with self.database.connection() as connection:
            return dict(
                connection.execute(
                    "SELECT item_id,starred FROM favorites WHERE user_id=?",
                    (user_id,),
                ).fetchall()
            )

    def schedule(self, connection):
        connection.execute(
            "INSERT OR IGNORE INTO save_jobs(source,policy,state) "
            "SELECT DISTINCT c.source,?,'pending' FROM retained_audio t "
            "JOIN catalog c ON c.id=t.recording_id",
            (self.policy,),
        )
        connection.execute(
            "DELETE FROM save_jobs WHERE policy!=? OR source NOT IN "
            "(SELECT c.source FROM retained_audio t "
            "JOIN catalog c ON c.id=t.recording_id)",
            (self.policy,),
        )

    def change(self, user_id, prepared, enabled, imported, now):
        with self.database.transaction() as connection:
            if imported is not None:
                target, key = imported
                active = connection.execute(
                    "SELECT 1 FROM love_imports WHERE user_id=? "
                    "AND target=? AND state='active'",
                    (user_id, target),
                ).fetchone()
                removed = connection.execute(
                    "SELECT 1 FROM love_removals WHERE user_id=? "
                    "AND recording_key=?",
                    (user_id, key),
                ).fetchone()
                if not active or removed:
                    return False
                manual = connection.execute(
                    "SELECT c.source FROM loved_items l JOIN catalog c "
                    "ON c.id=l.manual_song_id WHERE l.user_id=? "
                    "AND l.target=? "
                    "AND l.recording_key=?",
                    (user_id, target, key),
                ).fetchone()
                if manual:
                    return False
            changed = False
            for item_id, data in prepared.items():
                tracks = data["tracks"]
                stored = data["stored"]
                document = stored["document"]
                key = (
                    recording_key(
                        document.get("artist", ""), document["title"]
                    )
                    if stored["kind"] == "song"
                    else None
                )
                if imported is not None:
                    if connection.execute(
                        "SELECT 1 FROM favorite_removals WHERE user_id=? "
                        "AND item_id=?",
                        (user_id, item_id),
                    ).fetchone():
                        return False
                else:
                    if key is not None and self.love_intents is not None:
                        self.love_intents.record(
                            connection, user_id, stored, enabled
                        )
                    if enabled:
                        connection.execute(
                            "DELETE FROM favorite_removals "
                            "WHERE user_id=? AND item_id=?",
                            (user_id, item_id),
                        )
                    else:
                        connection.execute(
                            "INSERT OR IGNORE INTO favorite_removals "
                            "VALUES(?,?)",
                            (user_id, item_id),
                        )
                    if key is not None:
                        if enabled:
                            connection.execute(
                                "DELETE FROM love_removals WHERE user_id=? "
                                "AND (recording_key=? OR recording_key IN "
                                "(SELECT recording_key FROM loved_items "
                                "WHERE user_id=? AND song_id=?))",
                                (user_id, key, user_id, item_id),
                            )
                        else:
                            connection.execute(
                                "INSERT OR IGNORE INTO love_removals "
                                "SELECT user_id,recording_key "
                                "FROM loved_items "
                                "WHERE user_id=? AND song_id=?",
                                (user_id, item_id),
                            )
                            connection.execute(
                                "INSERT OR IGNORE INTO love_removals "
                                "VALUES(?,?)",
                                (user_id, key),
                            )
                            connection.execute(
                                "UPDATE loved_items SET state='skipped', "
                                "error='Removed locally.' WHERE user_id=? "
                                "AND (recording_key=? OR song_id=?)",
                                (user_id, key, item_id),
                            )
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
            self.schedule(connection)
        return True

    def collection_ids(self, user_id, starred_only):
        with self.database.connection() as connection:
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
                        "SELECT DISTINCT song_id FROM retained_tracks "
                        "WHERE user_id=?",
                        (user_id,),
                    )
                )

        return ids
