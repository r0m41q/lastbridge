"""Import previews, access rules and one refresh per shared source."""

import asyncio
import logging
import time
from datetime import UTC, datetime

from lastbridge.features.playlists.application.import_ports import (
    ImportCatalog,
    ImportLibrary,
    ImportStore,
    SnapshotProvider,
)
from lastbridge.features.playlists.domain.imports import (
    Snapshot,
    lastfm_source,
    playlist_source,
    refresh_days,
)
from lastbridge.features.playlists.domain.rules import label
from lastbridge.shared.errors import CatalogError, StorageError

logger = logging.getLogger(__name__)


def timestamp(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


class Imports:
    def __init__(
        self,
        repository: ImportStore,
        provider: SnapshotProvider,
        catalog: ImportCatalog,
        library: ImportLibrary,
        lastfm_provider: SnapshotProvider | None = None,
    ):
        self.repository, self.provider = repository, provider
        self.catalog, self.library = catalog, library
        self.lastfm_provider = lastfm_provider
        self.task = None
        self.slot = asyncio.Lock()
        self.wake = asyncio.Event()
        self.active = None

    def rows(self, user):
        return self.repository.visible(user)

    def get(self, user, item_id):
        return self.repository.get(user, item_id)

    def document(self, user, row, detail=False):
        ids = [e["song_id"] for e in row["entries"] if e.get("song_id")]
        documents = {
            s: self.catalog.read(s, "song")["document"] for s in set(ids)
        }
        songs = self.library.decorate(user["id"], [documents[s] for s in ids])
        result = {
            "id": row["id"],
            "name": row["title"],
            "comment": "Last.fm import"
            if lastfm_source(row["source"])
            else "YouTube import",
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
            result["entry"] = songs
        return result

    async def preview(self, user, url, shared=False):
        if shared and not user["admin"]:
            raise CatalogError(50, "Administrator access required")
        source = playlist_source(
            url,
            user.get("lastfm_name")
            if user.get("link_status") in ("linked", "relink")
            else None,
        )
        if lastfm_source(source) and shared:
            raise CatalogError(10, "Last.fm playlist imports must be private")
        item_id = self.repository.enqueue(user, source, shared)
        self.wake.set()
        return item_id

    def queue(self, user, offset=0, limit=50):
        return self.repository.queue(user, offset, limit)

    def control(self, user, item_id, action):
        self.repository.control(user, item_id, action)
        self.cancel_active(item_id)
        self.wake.set()

    def decide(
        self,
        user,
        item_id,
        source_key,
        song_id,
        acknowledge=False,
        action="choose",
        undo="",
    ):
        self.writable(user, item_id)
        return self.repository.decide(
            user, item_id, source_key, song_id, acknowledge, action, undo
        )

    def cancel_active(self, item_id):
        if (
            self.active
            and self.active[0] == item_id
            and not self.active[1].cancelling()
        ):
            self.active[1].cancel()

    def retry_shared(self, item_id):
        self.repository.retry_shared(item_id)
        self.wake.set()

    def rename_shared(self, user, item_id, name):
        if not user["admin"]:
            raise CatalogError(50, "Administrator access required")
        name = label(name, "name", 200, empty=True).strip()
        self.repository.rename_shared(item_id, name or None)

    def rename(self, user, item_id, name):
        row = self.writable(user, item_id)
        if not row["ready"]:
            raise CatalogError(10, "Confirm the preview first")
        if row["shared"]:
            self.rename_shared(user, item_id, name)
            return
        name = label(name, "name", 200, empty=True).strip()
        self.repository.rename_private(user["id"], item_id, name or None)

    def schedule_shared(self, user, item_id, days):
        if not user["admin"]:
            raise CatalogError(50, "Administrator access required")
        self.repository.schedule_shared(item_id, refresh_days(days))
        self.wake.set()

    def schedule(self, user, item_id, days):
        row = self.writable(user, item_id)
        if not row["ready"]:
            raise CatalogError(10, "Confirm the preview first")
        if row["shared"]:
            self.schedule_shared(user, item_id, days)
            return
        if not lastfm_source(row["source"]):
            raise CatalogError(10, "This import refreshes manually")
        self.repository.schedule_private(
            user["id"], item_id, refresh_days(days)
        )
        self.wake.set()

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
        self.cancel_active(item_id)

    async def refresh(self, user, item_id):
        row = self.writable(user, item_id)
        if not row["ready"]:
            raise CatalogError(10, "Confirm the preview first")
        self.repository.request(item_id)
        self.wake.set()

    async def load(self, job):
        started = time.monotonic()
        progress = job["progress"]
        phase = job["phase"]
        logger.info(
            "Playlist import started id=%s phase=%s processed=%s total=%s",
            job["id"],
            phase,
            job["processed"],
            job["total"],
        )
        try:
            provider = self.provider
            if lastfm_source(job["source"]):
                self.repository.verify_lastfm_owner(job)
                provider = self.lastfm_provider
                if provider is None:
                    raise CatalogError(0, "Last.fm imports unavailable")
            if phase == "fetch":
                progress = await provider.prepare(job["source"], progress)
                if not self.repository.checkpoint(job, progress):
                    return False
                phase = "resolve"
            while len(progress["entries"]) < len(progress["tracks"]):
                progress = await provider.batch(progress)
                if not self.repository.checkpoint(job, progress):
                    return False
                logger.debug(
                    "Playlist import progress id=%s processed=%s total=%s",
                    job["id"],
                    len(progress["entries"]),
                    len(progress["tracks"]),
                )
                await asyncio.sleep(0)
            job["progress"] = progress
            published = self.repository.publish(
                job, Snapshot(progress["title"], progress["entries"])
            )
            if published:
                logger.info(
                    "Playlist import completed id=%s processed=%s "
                    "elapsed_ms=%s",
                    job["id"],
                    len(progress["entries"]),
                    int((time.monotonic() - started) * 1000),
                )
            return published
        except asyncio.CancelledError:
            # Token fencing leaves an explicit pause/cancel untouched.
            self.repository.release(job)
            raise
        except (CatalogError, TimeoutError, StorageError) as error:
            if isinstance(error, CatalogError):
                reason = error.message
                retry = error.code == 0
            elif isinstance(error, TimeoutError):
                reason, retry = "Provider request timed out", True
            else:
                reason, retry = "Could not save import progress", True
            self.repository.failed(job, f"{phase}: {reason}", retry=retry)
            logger.warning(
                "Playlist import failed id=%s phase=%s processed=%s "
                "error_type=%s code=%s elapsed_ms=%s",
                job["id"],
                phase,
                len(progress.get("entries", [])),
                type(error).__name__,
                getattr(error, "code", "-"),
                int((time.monotonic() - started) * 1000),
            )
            return False
        except Exception as error:
            self.repository.failed(
                job, f"{phase}: Unexpected import worker error", retry=False
            )
            logger.error(
                "Playlist import worker error id=%s phase=%s error_type=%s",
                job["id"],
                phase,
                type(error).__name__,
            )
            return False

    async def refresh_one(self):
        if self.slot.locked():
            return False
        async with self.slot:
            job = self.repository.claim()
            if job is None:
                return False
            task = asyncio.create_task(self.load(job))
            self.active = (job["id"], task)
            try:
                await task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            finally:
                self.active = None
            return True

    async def worker(self):
        while True:
            self.wake.clear()
            try:
                worked = await self.refresh_one()
            except StorageError:
                logger.warning("Playlist import storage unavailable")
                worked = False
            if worked:
                continue
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=5)
            except TimeoutError:
                pass

    async def start(self):
        self.repository.recover()
        self.task = asyncio.create_task(self.worker())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
