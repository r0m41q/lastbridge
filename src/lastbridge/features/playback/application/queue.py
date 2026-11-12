import asyncio
import logging
from datetime import UTC, datetime, timedelta

from lastbridge.features.playback.application.ports import (
    QueueAudio,
    QueueCatalog,
    QueueStore,
)
from lastbridge.shared.errors import CatalogError, StorageError

logger = logging.getLogger(__name__)


class PlayQueue:
    def __init__(
        self, repository: QueueStore, catalog: QueueCatalog, audio: QueueAudio
    ):
        self.repository, self.catalog, self.audio = repository, catalog, audio
        self.tasks = {}
        self.closed = False

    def get(self, user_id):
        document = self.repository.get(user_id)
        if document is None:
            return {"entry": []}
        return {k: v for k, v in document.items() if k != "ids"} | {
            "entry": [
                self.catalog.read(song_id, "song")["document"]
                for song_id in document["ids"]
            ]
        }

    def save(self, user, ids, current, position, client):
        if len(ids) > 1000:
            raise CatalogError(10, "Play queue supports at most 1000 songs")
        if ids and current not in ids:
            raise CatalogError(10, "Current song must be in the play queue")
        if not ids and current:
            raise CatalogError(10, "Current song must be in the play queue")
        for song_id in set(ids):
            self.catalog.read(song_id, "song")
        document = {
            "ids": ids,
            "position": position if ids else 0,
            "username": user["username"],
            "changed": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "changedBy": client,
        }
        if ids:
            document["current"] = current
        self.repository.save(user["id"], document)
        self.cancel(user["id"])
        self.playing(user["id"], client, current)

    def cancel(self, user_id):
        task = self.tasks.pop(user_id, None)
        if task is not None:
            task.cancel()

    def playing(self, user_id, client, song_id):
        if self.closed:
            return
        try:
            document = self.repository.get(user_id)
            if not document or document["changedBy"] != client:
                return
            changed = datetime.fromisoformat(document["changed"])
            if datetime.now(UTC) - changed > timedelta(hours=1):
                return
            ids = document["ids"]
            # ID-based queues cannot identify an occurrence of a repeat.
            if ids.count(song_id) != 1:
                return
            index = ids.index(song_id) + 1
            self.cancel(user_id)
            if index >= len(ids) or len(self.tasks) >= 32:
                return
            source = self.catalog.read(ids[index], "song")["source"]
            task = asyncio.create_task(self.warm(source))
            self.tasks[user_id] = task

            def finished(done):
                if self.tasks.get(user_id) is done:
                    self.tasks.pop(user_id, None)

            task.add_done_callback(finished)
        except CatalogError, StorageError:
            # Queue hints never prevent foreground playback.
            logger.debug("Play queue prefetch unavailable")

    async def warm(self, source):
        key = None
        try:
            key, _ = await self.audio.acquire(source, prefetch=True)
        except CatalogError, StorageError, OSError:
            logger.debug("Play queue prefetch failed source=%s", source)
        finally:
            if key is not None:
                await self.audio.release(key)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
