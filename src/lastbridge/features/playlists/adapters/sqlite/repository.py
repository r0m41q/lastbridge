from contextlib import contextmanager
from datetime import UTC, datetime

from lastbridge.features.playlists.adapters.sqlite.retention import (
    RetentionWriter,
)


class PlaylistRepository:
    def __init__(self, database, retention: RetentionWriter):
        self.database, self.retention = database, retention

    @contextmanager
    def edit(self):
        with self.database.transaction() as connection:
            yield PlaylistEdit(self, connection)

    def local(self, user):
        with self.database.connection() as connection:
            return [
                (dict(row), self.ids(connection, row["id"]))
                for row in connection.execute(
                    "SELECT * FROM playlists WHERE user_id=? ORDER BY name "
                    "COLLATE NOCASE,id",
                    (user["id"],),
                )
            ]

    def get(self, user, item_id):
        with self.database.connection() as connection:
            row = self.row(user, item_id, connection)
            return (dict(row), self.ids(connection, item_id)) if row else None

    def row(self, user, item_id, connection):
        return connection.execute(
            "SELECT * FROM playlists WHERE id=? AND user_id=?",
            (item_id, user["id"]),
        ).fetchone()

    @staticmethod
    def ids(connection, item_id):
        return [
            r[0]
            for r in connection.execute(
                "SELECT song_id FROM playlist_entries WHERE playlist_id=? "
                "ORDER BY position",
                (item_id,),
            )
        ]

    def entries(self, connection, item_id, ids):
        connection.execute(
            "DELETE FROM playlist_entries WHERE playlist_id=?", (item_id,)
        )
        connection.executemany(
            "INSERT INTO playlist_entries VALUES(?,?,?)",
            [(item_id, index, song) for index, song in enumerate(ids)],
        )

    def schedule(self, connection, user):
        self.retention.schedule(connection)
        connection.execute(
            "UPDATE users SET library_changed=MAX(library_changed+1,?) "
            "WHERE id=?",
            (int(datetime.now(UTC).timestamp() * 1000), user["id"]),
        )

    def downloaded(self, user):
        with self.database.connection() as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT id FROM playlists WHERE user_id=? "
                    "AND downloaded=1",
                    (user["id"],),
                )
            }


class PlaylistEdit:
    def __init__(self, repository, connection):
        self.repository, self.connection = repository, connection

    def row(self, user, item_id):
        row = self.repository.row(user, item_id, self.connection)
        return dict(row) if row else None

    def count(self, user_id):
        return self.connection.execute(
            "SELECT count(*) FROM playlists WHERE user_id=?", (user_id,)
        ).fetchone()[0]

    def insert(self, item_id, user_id, name, stamp):
        self.connection.execute(
            "INSERT INTO playlists(id,user_id,name,created,changed) "
            "VALUES(?,?,?,?,?)",
            (item_id, user_id, name, stamp, stamp),
        )

    def rename(self, item_id, name, stamp):
        self.connection.execute(
            "UPDATE playlists SET name=?,changed=? WHERE id=?",
            (name, stamp, item_id),
        )

    def ids(self, item_id):
        return self.repository.ids(self.connection, item_id)

    def entries(self, item_id, ids):
        self.repository.entries(self.connection, item_id, ids)

    def update(self, item_id, name, comment, stamp):
        self.connection.execute(
            "UPDATE playlists SET name=?,comment=?,changed=? WHERE id=?",
            (name, comment, stamp, item_id),
        )

    def delete(self, item_id):
        self.connection.execute("DELETE FROM playlists WHERE id=?", (item_id,))

    def keep(self, item_id, enabled, stamp):
        self.connection.execute(
            "UPDATE playlists SET downloaded=?,changed=? WHERE id=?",
            (int(enabled), stamp, item_id),
        )

    def schedule(self, user):
        self.repository.schedule(self.connection, user)
