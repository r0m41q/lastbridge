import asyncio
import contextlib
from datetime import UTC, datetime

from lastbridge.features.library.application.ports import (
    AudioStorage,
    CatalogAccess,
    LibraryStore,
)
from lastbridge.features.library.application.worker import DownloadWorker
from lastbridge.features.library.domain.documents import decorate
from lastbridge.shared.errors import CatalogError


class Library:
    def __init__(
        self, repository: LibraryStore, catalog: CatalogAccess, policy
    ):
        self.repository, self.catalog, self.policy = (
            repository,
            catalog,
            policy,
        )
        self.audio: AudioStorage | None = None
        self.wake = asyncio.Event()
        self.worker = None

    def sources(self):
        return self.repository.sources()

    def pins(self):
        return {f"{source}-{self.policy}" for source in self.sources()}

    def stars(self, user_id):
        return self.repository.stars(user_id)

    def decorate(self, user_id, document):
        return decorate(
            document, self.stars(user_id), self.repository.plays(user_id)
        )

    async def change(self, user_id, targets, enabled, *, imported=None):
        if not targets:
            raise CatalogError(10, "Supply id, albumId or artistId")
        # Validate the whole batch before changing private membership. Album
        # metadata is resolved first; a failed lookup cannot half-save a batch.
        prepared = {}
        for item_id, kind in targets:
            stored = self.catalog.read(item_id, kind)
            tracks = []
            if enabled:
                if stored["kind"] == "song":
                    tracks = [item_id]
                elif stored["kind"] == "album":
                    album = await self.catalog.detail(item_id, "album")
                    tracks = [song["id"] for song in album.get("song", [])]
            prepared[item_id] = {"tracks": tracks, "stored": stored}
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        if not self.repository.change(
            user_id, prepared, enabled, imported, now
        ):
            return False
        await self.reconcile()
        self.wake.set()
        return True

    def notify(self):
        self.wake.set()

    async def reconcile(self, startup=False):
        await self.audio.update_pins(self.pins())
        # Re-read after the storage lock may have yielded to a newer edit.
        sources = self.sources()
        saved = {
            key
            for key, entry in self.audio.entries.items()
            if entry.get("saved")
        }
        self.repository.reconcile(sources, saved, startup)

    async def start(self, audio, *, worker=True):
        self.audio = audio
        await self.reconcile(startup=True)
        if worker:
            self.worker = asyncio.create_task(self.run())

    async def close(self):
        if self.worker is not None:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker

    async def run(self):
        await DownloadWorker(self).run()

    async def process_one(self):
        job = self.repository.claim()
        if job is None:
            return False
        key = None
        try:
            key, _ = await self.audio.acquire(job["source"], role="download")
            saved = await self.audio.retain(key)
            if saved:
                self.repository.complete(job["source"])
        except (CatalogError, OSError) as error:
            # Fixed message only: provider URLs and dependency exception text
            # must not enter the database or another user's browser page.
            attempts = job["attempts"] + 1
            message = "Download failed; check free disk space or retry later."
            reasons = {
                "Not enough free space for audio",
                "Recording exceeds the audio file size limit",
                "Recording unavailable from audio provider",
                "Requested source audio unavailable",
                "Audio provider denied access; try later",
                "Audio provider busy; retry shortly",
                "Audio cache busy; retry shortly",
                "Audio acquisition timed out; retry",
            }
            if isinstance(error, CatalogError) and error.message in reasons:
                message = "Download failed: " + error.message
            self.repository.failed(job["source"], attempts, message)
        finally:
            if key is not None:
                await self.audio.release(key)
        return True

    def retry(self, user_id):
        self.repository.retry(user_id)
        self.wake.set()

    def downloads(self, user_id):
        return [
            row | {"title": self.catalog.read(row["id"])["document"]["title"]}
            for row in self.repository.downloads(user_id)
        ]

    def collection(self, user_id, starred_only=False):
        stars = self.stars(user_id)
        ids = self.repository.collection_ids(user_id, starred_only)
        items = {item_id: self.catalog.read(item_id) for item_id in ids}
        if not starred_only:
            # Only include album/artist parents of this user's saved songs.
            # Public catalog searches never constitute library membership.
            for item in list(items.values()):
                for field in ("albumId", "artistId"):
                    parent = item["document"].get(field)
                    if parent and parent not in items:
                        items[parent] = self.catalog.read(parent)
            for item in list(items.values()):
                parent = item["document"].get("artistId")
                if parent and parent not in items:
                    items[parent] = self.catalog.read(parent)
        result = {"artist": [], "album": [], "song": []}
        song_docs = [
            r["document"] for r in items.values() if r["kind"] == "song"
        ]
        for item_id, item in items.items():
            kind = item["kind"]
            doc = {
                k: v
                for k, v in item["document"].items()
                if k not in ("starred", "song")
                and not (k == "album" and isinstance(v, list))
            }
            if item_id in stars:
                doc["starred"] = stars[item_id]
            if not starred_only and kind == "album":
                songs = [s for s in song_docs if s.get("albumId") == item_id]
                doc["songCount"] = len(songs)
                doc["duration"] = sum(s.get("duration", 0) for s in songs)
            if not starred_only and kind == "artist":
                doc["albumCount"] = sum(
                    r["kind"] == "album"
                    and r["document"].get("artistId") == item_id
                    for r in items.values()
                )
            result[kind].append(doc)
        for kind in result:
            result[kind].sort(
                key=lambda d: (
                    d.get("name", d.get("title", "")).casefold(),
                    d["id"],
                )
            )
        return result
