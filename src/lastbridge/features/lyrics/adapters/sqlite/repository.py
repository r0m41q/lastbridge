import json
import time


class LyricsRepository:
    def __init__(self, database):
        self.database = database

    def cached(self, song_id, fingerprint):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM lyrics WHERE song_id=? AND fingerprint=?",
                (song_id, fingerprint),
            ).fetchone()
        return dict(row) if row else None

    def save(self, song_id, fingerprint, document, ttl, updated=None):
        now = time.time()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO lyrics VALUES(?,?,?,?,?) ON CONFLICT(song_id) "
                "DO UPDATE SET fingerprint=excluded.fingerprint, "
                "document=excluded.document, expires=excluded.expires, "
                "updated=excluded.updated",
                (
                    song_id,
                    fingerprint,
                    json.dumps(document, ensure_ascii=False),
                    now + ttl,
                    updated if updated is not None else now,
                ),
            )
            connection.execute(
                "DELETE FROM lyrics WHERE song_id IN (SELECT song_id FROM "
                "lyrics ORDER BY updated DESC, song_id LIMIT -1 OFFSET 1000)"
            )
