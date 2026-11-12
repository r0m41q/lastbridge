import asyncio
import contextlib
import logging
from pathlib import PurePosixPath

from lastbridge.features.playback.application.ports import (
    AlbumCovers,
    ArtworkSource,
    MediaFiles,
    ReadyNotifier,
)
from lastbridge.shared.errors import CatalogError, StorageError

logger = logging.getLogger(__name__)


class SavedArtwork:
    """Populate covers for managed saved audio without delaying acquisition."""

    def __init__(
        self,
        repository: MediaFiles,
        source: ArtworkSource,
        covers: AlbumCovers,
        notifier: ReadyNotifier | None = None,
        enrichment=None,
    ):
        self.repository, self.source, self.covers = repository, source, covers
        self.notifier = notifier
        self.enrichment = enrichment
        self.wake = asyncio.Event()
        self.task = None
        self.scan_pending = True

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def notify(self):
        self.scan_pending = True
        self.wake.set()

    async def populate(self):
        changed, completed = False, set()
        # SQLite is the inventory; no recursive library discovery is needed.
        for record in self.repository.list():
            if record.state != "ready" or not record.relative_path:
                continue
            if self.enrichment is not None:
                changed = await self.enrichment.populate(record) or changed
            album = str(PurePosixPath(record.relative_path).parent)
            if album in completed:
                continue
            try:
                if not self.covers.needed(record.relative_path):
                    continue
                data, media_type = await self.source.fetch(record.source)
                current = self.repository.get(record.source, record.policy)
                if (
                    current is None
                    or current.state != "ready"
                    or current.relative_path != record.relative_path
                ):
                    continue
                if self.covers.write(record.relative_path, data, media_type):
                    changed = True
                    completed.add(album)
            except CatalogError, StorageError, OSError, ValueError:
                # Another track in this album can supply a missing cover.
                # Failures remain retryable and never invalidate saved audio.
                logger.warning("Saved album artwork unavailable; will retry")
        if self.notifier is not None and (changed or self.scan_pending):
            self.scan_pending = False
            await self.notifier.notify()
        return changed

    async def run(self):
        while True:
            self.wake.clear()
            try:
                await self.populate()
            except StorageError, OSError:
                logger.warning("Saved artwork storage unavailable; will retry")
            try:
                await asyncio.wait_for(self.wake.wait(), 300)
            except TimeoutError:
                pass

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        if self.notifier is not None:
            await self.notifier.close()
