"""Atomic import snapshots and durable refresh leases."""

import json
import time
from uuid import uuid4

from lastbridge.features.playlists.domain.imports import DAY
from lastbridge.shared.errors import CatalogError


class ImportRepository:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def decode(row):
        return dict(row) | {"entries": json.loads(row["entries"])}

    def visible(self, user):
        with self.database.connection() as connection:
            return [
                self.decode(row)
                for row in connection.execute(
                    "SELECT * FROM playlist_imports WHERE ready=1 AND "
                    "(shared=1 OR owner_id=?) ORDER BY created,id",
                    (user["id"],),
                )
            ]

    def get(self, user, item_id):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM playlist_imports WHERE id=? AND "
                "((ready=1 AND shared=1) OR owner_id=?)",
                (item_id, user["id"]),
            ).fetchone()
        if row is None or (
            not row["ready"] and row["created"] < time.time() - 3600
        ):
            raise CatalogError(70, "Import not found")
        return self.decode(row)

    def draft(self, user, source, shared, snapshot):
        now, item_id = time.time(), "y" + uuid4().hex
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM playlist_imports WHERE ready=0 AND created<?",
                (now - 3600,),
            )
            count = connection.execute(
                "SELECT count(*) FROM playlist_imports WHERE owner_id=?",
                (user["id"],),
            ).fetchone()[0]
            if count >= 200:
                raise CatalogError(10, "Limit of 200 imports per account")
            connection.execute(
                "INSERT INTO playlist_imports "
                "(id,owner_id,source,shared,title,entries,created,updated,"
                "next_attempt) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    user["id"],
                    source,
                    int(shared),
                    snapshot.title,
                    json.dumps(snapshot.entries),
                    now,
                    now,
                    now + DAY,
                ),
            )
        return item_id

    def confirm(self, user, item_id):
        # Recheck the draft within the write transaction: a repeated submit
        # cannot overwrite a refresh or create duplicate shared charts.
        now = time.time()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM playlist_imports WHERE id=? AND owner_id=? "
                "AND ready=0 AND created>=?",
                (item_id, user["id"], now - 3600),
            ).fetchone()
            if row is None:
                raise CatalogError(70, "Preview expired; start again")
            if row["shared"] and not user["admin"]:
                raise CatalogError(50, "Administrator access required")
            existing = connection.execute(
                "SELECT id FROM playlist_imports WHERE source=? AND "
                "shared=? AND ready=1 AND (shared=1 OR owner_id=?)",
                (row["source"], row["shared"], user["id"]),
            ).fetchone()
            if existing:
                connection.execute(
                    "DELETE FROM playlist_imports WHERE id=?", (item_id,)
                )
                return existing["id"]
            connection.execute(
                "UPDATE playlist_imports SET ready=1,updated=?,"
                "next_attempt=? WHERE id=?",
                (now, now + DAY, item_id),
            )
        return item_id

    def claim(self, item_id=None):
        now, token = time.time(), uuid4().hex
        with self.database.transaction() as connection:
            if item_id is None:
                row = connection.execute(
                    "SELECT * FROM playlist_imports WHERE ready=1 AND "
                    "shared=1 AND next_attempt<=? ORDER BY next_attempt "
                    "LIMIT 1",
                    (now,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM playlist_imports WHERE id=? AND ready=1 "
                    "AND token IS NULL AND (error IS NULL OR "
                    "next_attempt<=?)",
                    (item_id, now),
                ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE playlist_imports SET token=?,next_attempt=? "
                "WHERE id=?",
                (token, now + 600, row["id"]),
            )
        return dict(row) | {"token": token}

    def publish(self, job, snapshot):
        now = time.time()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE playlist_imports SET title=?,entries=?,updated=?,"
                "next_attempt=?,token=NULL,error=NULL WHERE id=? AND token=?",
                (
                    snapshot.title,
                    json.dumps(snapshot.entries),
                    now,
                    now + DAY,
                    job["id"],
                    job["token"],
                ),
            )

    def failed(self, job):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE playlist_imports SET next_attempt=?,token=NULL,"
                "error='Refresh failed; previous snapshot kept' "
                "WHERE id=? AND token=?",
                (time.time() + 300, job["id"], job["token"]),
            )

    def remove(self, item_id):
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM playlist_imports WHERE id=?", (item_id,)
            )

    def recover(self):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE playlist_imports SET token=NULL,next_attempt=? "
                "WHERE token IS NOT NULL",
                (time.time() + 60,),
            )
            connection.execute(
                "DELETE FROM playlist_imports WHERE ready=0 AND created<?",
                (time.time() - 3600,),
            )
