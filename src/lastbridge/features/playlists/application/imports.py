"""Import previews, access rules and one refresh per shared source."""

import asyncio
from datetime import UTC, datetime

from lastbridge.features.playlists.application.import_ports import (
    ImportCatalog,
    ImportLibrary,
    ImportStore,
    SnapshotProvider,
)
from lastbridge.features.playlists.domain.imports import playlist_source
from lastbridge.shared.errors import CatalogError, StorageError


def timestamp(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


class Imports:
    def __init__(
        self,
        repository: ImportStore,
        provider: SnapshotProvider,
        catalog: ImportCatalog,
        library: ImportLibrary,
    ):
        self.repository, self.provider = repository, provider
        self.catalog, self.library = catalog, library
        self.task = None
        self.slot = asyncio.Lock()

    def rows(self, user):
        return self.repository.visible(user)

    def get(self, user, item_id):
        return self.repository.get(user, item_id)

    def document(self, user, row, detail=False):
        ids = [e["song_id"] for e in row["entries"] if e.get("song_id")]
        documents = {
            s: self.catalog.read(s, "song")["document"] for s in set(ids)
        }
        songs = [documents[s] for s in ids]
        result = {
            "id": row["id"],
            "name": row["title"],
            "comment": "YouTube import",
            "owner": "Lastbridge" if row["shared"] else user["username"],
            "public": bool(row["shared"]),
            "readonly": True,
            "created": timestamp(row["created"]),
            "changed": timestamp(row["updated"]),
            "songCount": len(songs),
            "duration": sum(s.get("duration", 0) for s in songs),
        }
        if songs and songs[0].get("coverArt"):
            result["coverArt"] = songs[0]["coverArt"]
        if detail:
            result["entry"] = self.library.decorate(user["id"], songs)
        return result

    async def preview(self, user, url, shared=False):
        if shared and not user["admin"]:
            raise CatalogError(50, "Administrator access required")
        source = playlist_source(url)
        if self.slot.locked():
            raise CatalogError(0, "Playlist importer busy; retry shortly")
        async with self.slot, asyncio.timeout(180):
            snapshot = await self.provider.load(source)
        return self.repository.draft(user, source, shared, snapshot)

    def confirm(self, user, item_id):
        return self.repository.confirm(user, item_id)

    def writable(self, user, item_id):
        row = self.get(user, item_id)
        if row["shared"] and not user["admin"]:
            raise CatalogError(50, "Administrator access required")
        return row

    def remove(self, user, item_id):
        self.writable(user, item_id)
        self.repository.remove(item_id)

    async def refresh(self, user, item_id):
        row = self.writable(user, item_id)
        if not row["ready"]:
            raise CatalogError(10, "Confirm the preview first")
        if self.slot.locked():
            raise CatalogError(0, "Playlist importer busy; retry shortly")
        async with self.slot:
            job = self.repository.claim(item_id)
            if job is None:
                raise CatalogError(0, "Refresh pending; retry shortly")
            if not await self.load(job):
                raise CatalogError(0, "Refresh failed; previous snapshot kept")

    async def load(self, job):
        try:
            async with asyncio.timeout(180):
                snapshot = await self.provider.load(job["source"])
            self.repository.publish(job, snapshot)
        except asyncio.CancelledError:
            self.repository.failed(job)
            raise
        except CatalogError, TimeoutError, StorageError:
            self.repository.failed(job)
            return False
        return True

    async def refresh_one(self):
        if self.slot.locked():
            return False
        async with self.slot:
            job = self.repository.claim()
            if job is None:
                return False
            await self.load(job)
            return True

    async def worker(self):
        while True:
            try:
                worked = await self.refresh_one()
            except StorageError:
                worked = False
            await asyncio.sleep(0 if worked else 30)

    async def start(self):
        self.repository.recover()
        self.task = asyncio.create_task(self.worker())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
