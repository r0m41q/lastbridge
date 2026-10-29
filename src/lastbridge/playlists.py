from datetime import UTC, datetime, timedelta
from uuid import uuid4

from lastbridge.catalog import CatalogError

MAX_TRACKS = 1000
MAX_PLAYLISTS = 200


def label(value, field, maximum, *, empty=False):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not empty and not value.strip())
        or any(
            not (
                c in "\t\n\r"
                or 0x20 <= ord(c) <= 0xD7FF
                or 0xE000 <= ord(c) <= 0xFFFD
                or 0x10000 <= ord(c) <= 0x10FFFF
            )
            for c in value
        )
    ):
        raise CatalogError(10, f"Invalid parameter: {field}")
    return value


class Playlists:
    def __init__(self, accounts, catalog, library, mixes, radio):
        self.accounts, self.catalog, self.library = accounts, catalog, library
        self.mixes, self.radio = mixes, radio

    def row(self, user, item_id, connection):
        return connection.execute(
            "SELECT * FROM playlists WHERE id=? AND user_id=?",
            (item_id, user["id"]),
        ).fetchone()

    def editable(self, user, item_id, connection):
        row = self.row(user, item_id, connection)
        if row is None:
            # A caller's own generated list is read-only. Foreign IDs reveal
            # neither existence nor ownership, even to administrators.
            self.get(user, item_id)
            raise CatalogError(50, "Generated playlists are read only")
        return row

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

    def document(self, user, row, ids, detail):
        documents = {
            s: self.catalog.read(s, "song")["document"] for s in set(ids)
        }
        songs = [documents[s] for s in ids]
        result = {
            "id": row["id"],
            "name": row["name"],
            "comment": row["comment"],
            "owner": user["username"],
            "public": False,
            "readonly": False,
            "created": row["created"],
            "changed": row["changed"],
            "songCount": len(songs),
            "duration": sum(s.get("duration", 0) for s in songs),
        }
        if songs and songs[0].get("coverArt"):
            result["coverArt"] = songs[0]["coverArt"]
        if detail:
            result["entry"] = self.library.decorate(user["id"], songs)
        return result

    def local(self, user):
        with self.accounts.connection() as connection:
            return [
                self.document(
                    user, row, self.ids(connection, row["id"]), False
                )
                for row in connection.execute(
                    "SELECT * FROM playlists WHERE user_id=? "
                    "ORDER BY name COLLATE NOCASE,id",
                    (user["id"],),
                )
            ]

    def get(self, user, item_id):
        with self.accounts.connection() as connection:
            row = self.row(user, item_id, connection)
            if row is not None:
                return self.document(
                    user, row, self.ids(connection, item_id), True
                )
        if item_id == self.radio.playlist_id(user["id"]):
            result = self.radio.playlist(user, detail=True)
            if result is None:
                raise CatalogError(70, "Playlist not found")
            return result
        return self.mixes.playlists(user, item_id)

    def validate_songs(self, ids):
        if len(ids) > MAX_TRACKS:
            raise CatalogError(10, "Playlists support up to 1000 entries")
        for song_id in set(ids):
            self.catalog.read(song_id, "song")

    def touch(self, row):
        now = datetime.now(UTC)
        if row is not None:
            now = max(
                now,
                datetime.fromisoformat(row["changed"])
                + timedelta(milliseconds=1),
            )
        return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def entries(self, connection, item_id, ids):
        self.validate_songs(ids)
        connection.execute(
            "DELETE FROM playlist_entries WHERE playlist_id=?", (item_id,)
        )
        connection.executemany(
            "INSERT INTO playlist_entries VALUES(?,?,?)",
            [(item_id, index, song) for index, song in enumerate(ids)],
        )

    async def finish(self):
        # Pin intent and jobs have committed together. Reconciliation is
        # recoverable after cancellation or storage errors.
        await self.library.reconcile()
        self.library.wake.set()

    def schedule(self, connection, user):
        self.library.schedule(connection)
        connection.execute(
            "UPDATE users SET library_changed=MAX(library_changed+1,?) "
            "WHERE id=?",
            (int(datetime.now(UTC).timestamp() * 1000), user["id"]),
        )

    async def create(self, user, name, ids, item_id=None):
        self.validate_songs(ids)
        if name is not None:
            name = label(name, "name", 200)
        with self.accounts.transaction() as connection:
            if item_id is None:
                name = label(name, "name", 200)
                count = connection.execute(
                    "SELECT count(*) FROM playlists WHERE user_id=?",
                    (user["id"],),
                ).fetchone()[0]
                if count >= MAX_PLAYLISTS:
                    raise CatalogError(10, "Limit of 200 private playlists")
                item_id = "p" + uuid4().hex
                stamp = self.touch(None)
                connection.execute(
                    "INSERT INTO playlists(id,user_id,name,created,changed) "
                    "VALUES(?,?,?,?,?)",
                    (item_id, user["id"], name, stamp, stamp),
                )
            else:
                row = self.editable(user, item_id, connection)
                connection.execute(
                    "UPDATE playlists SET name=?,changed=? WHERE id=?",
                    (
                        name if name is not None else row["name"],
                        self.touch(row),
                        item_id,
                    ),
                )
            self.entries(connection, item_id, ids)
            self.schedule(connection, user)
        await self.finish()
        return self.get(user, item_id)

    async def update(self, user, item_id, values):
        name = label(values["name"], "name", 200) if "name" in values else None
        comment = (
            label(values["comment"], "comment", 2000, empty=True)
            if "comment" in values
            else None
        )
        if values.get("public", "false") not in ("true", "false"):
            raise CatalogError(10, "Invalid parameter: public")
        if values.get("public") == "true":
            raise CatalogError(50, "Playlist sharing is not supported")
        with self.accounts.transaction() as connection:
            row = self.editable(user, item_id, connection)
            ids = self.ids(connection, item_id)
            indices = set()
            for value in values.get("songIndexToRemove", []):
                if not value.isascii() or not value.isdecimal():
                    raise CatalogError(10, "Invalid songIndexToRemove")
                index = int(value)
                if not 0 <= index < len(ids):
                    raise CatalogError(10, "Invalid songIndexToRemove")
                indices.add(index)
            # Indices address the original list; additions follow removals.
            ids = [s for i, s in enumerate(ids) if i not in indices]
            ids += values.get("songIdToAdd", [])
            self.entries(connection, item_id, ids)
            connection.execute(
                "UPDATE playlists SET name=?,comment=?,changed=? WHERE id=?",
                (
                    name if name is not None else row["name"],
                    comment if comment is not None else row["comment"],
                    self.touch(row),
                    item_id,
                ),
            )
            self.schedule(connection, user)
        await self.finish()

    async def delete(self, user, item_id):
        with self.accounts.transaction() as connection:
            self.editable(user, item_id, connection)
            connection.execute("DELETE FROM playlists WHERE id=?", (item_id,))
            self.schedule(connection, user)
        await self.finish()

    async def keep(self, user, item_id, enabled):
        with self.accounts.transaction() as connection:
            row = self.editable(user, item_id, connection)
            connection.execute(
                "UPDATE playlists SET downloaded=?,changed=? WHERE id=?",
                (int(enabled), self.touch(row), item_id),
            )
            self.schedule(connection, user)
        await self.finish()

    async def copy(self, user, item_id, name):
        snapshot = self.get(user, item_id)
        return await self.create(
            user, name, [s["id"] for s in snapshot["entry"]]
        )

    def downloaded(self, user):
        with self.accounts.connection() as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT id FROM playlists WHERE user_id=? "
                    "AND downloaded=1",
                    (user["id"],),
                )
            }
