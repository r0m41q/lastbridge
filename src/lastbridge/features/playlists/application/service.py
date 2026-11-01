from uuid import uuid4

from lastbridge.features.playlists.application.ports import (
    CatalogAccess,
    LibraryAccess,
    MixAccess,
    PlaylistStore,
    RadioAccess,
)
from lastbridge.features.playlists.domain.rules import (
    MAX_PLAYLISTS,
    MAX_TRACKS,
    label,
    touch,
)
from lastbridge.shared.errors import CatalogError


class Playlists:
    def __init__(
        self,
        repository: PlaylistStore,
        catalog: CatalogAccess,
        library: LibraryAccess,
        mixes: MixAccess,
        radio: RadioAccess,
    ):
        self.repository, self.catalog, self.library = (
            repository,
            catalog,
            library,
        )
        self.mixes, self.radio = mixes, radio

    def local(self, user):
        return [
            self.document(user, row, ids, False)
            for row, ids in self.repository.local(user)
        ]

    def downloaded(self, user):
        return self.repository.downloaded(user)

    def editable(self, user, item_id, edit):
        row = edit.row(user, item_id)
        if row is None:
            # A caller's own generated list is read-only. Foreign IDs reveal
            # neither existence nor ownership, even to administrators.
            self.get(user, item_id)
            raise CatalogError(50, "Generated playlists are read only")
        return row

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

    def get(self, user, item_id):
        stored = self.repository.get(user, item_id)
        if stored is not None:
            row, ids = stored
            return self.document(user, row, ids, True)
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

    async def finish(self):
        # Pin intent and jobs have committed together. Reconciliation is
        # recoverable after cancellation or storage errors.
        await self.library.reconcile()
        self.library.notify()

    async def create(self, user, name, ids, item_id=None):
        self.validate_songs(ids)
        if name is not None:
            name = label(name, "name", 200)
        with self.repository.edit() as edit:
            if item_id is None:
                name = label(name, "name", 200)
                count = edit.count(user["id"])
                if count >= MAX_PLAYLISTS:
                    raise CatalogError(10, "Limit of 200 private playlists")
                item_id = "p" + uuid4().hex
                stamp = touch(None)
                edit.insert(item_id, user["id"], name, stamp)
            else:
                row = self.editable(user, item_id, edit)
                edit.rename(
                    item_id,
                    name if name is not None else row["name"],
                    touch(row),
                )
            self.validate_songs(ids)
            edit.entries(item_id, ids)
            edit.schedule(user)
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
        with self.repository.edit() as edit:
            row = self.editable(user, item_id, edit)
            ids = edit.ids(item_id)
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
            self.validate_songs(ids)
            edit.entries(item_id, ids)
            edit.update(
                item_id,
                name if name is not None else row["name"],
                comment if comment is not None else row["comment"],
                touch(row),
            )
            edit.schedule(user)
        await self.finish()

    async def delete(self, user, item_id):
        with self.repository.edit() as edit:
            self.editable(user, item_id, edit)
            edit.delete(item_id)
            edit.schedule(user)
        await self.finish()

    async def keep(self, user, item_id, enabled):
        with self.repository.edit() as edit:
            row = self.editable(user, item_id, edit)
            edit.keep(item_id, enabled, touch(row))
            edit.schedule(user)
        await self.finish()

    async def copy(self, user, item_id, name):
        snapshot = self.get(user, item_id)
        return await self.create(
            user, name, [s["id"] for s in snapshot["entry"]]
        )
