import asyncio
import logging
import time

from lastbridge.shared.errors import CatalogError, StorageError

logger = logging.getLogger(__name__)


class DownloadWorker:
    def __init__(self, library):
        self.library = library
        self.repository = library.repository
        self.wake = library.wake

    async def run(self):
        recover = False
        while True:
            self.wake.clear()
            try:
                if recover:
                    await self.library.reconcile(startup=True)
                    recover = False
                await self.library.process_one()
                due = self.repository.next_due()
            except StorageError, OSError, CatalogError:
                # If SQLite itself cannot write (for example disk exhaustion),
                # keep durable intents and retry after storage becomes usable.
                logger.warning("Save worker storage unavailable; retrying")
                recover = True
                await asyncio.sleep(5)
                continue
            if due is not None and due <= time.time():
                await asyncio.sleep(0)
                continue
            timeout = max(0.1, due - time.time()) if due is not None else None
            try:
                await asyncio.wait_for(self.wake.wait(), timeout)
            except TimeoutError:
                pass
