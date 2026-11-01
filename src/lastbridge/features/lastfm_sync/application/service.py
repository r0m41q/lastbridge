import asyncio
import contextlib

from lastbridge.features.lastfm_sync.application.delivery import (
    ScrobbleDelivery,
)
from lastbridge.features.lastfm_sync.application.imports import LovedImports
from lastbridge.features.lastfm_sync.application.ports import (
    CatalogAccess,
    LibraryAccess,
    SecretCipher,
    SyncProvider,
    SyncStore,
)
from lastbridge.features.lastfm_sync.application.reporting import (
    prepare_report,
)
from lastbridge.features.lastfm_sync.application.worker import run


class LastfmSync:
    def __init__(
        self,
        repository: SyncStore,
        catalog: CatalogAccess,
        library: LibraryAccess,
        remote: SyncProvider,
        cipher: SecretCipher,
    ):
        self.repository, self.catalog, self.library, self.remote = (
            repository,
            catalog,
            library,
            remote,
        )
        self.delivery = ScrobbleDelivery(repository, remote, cipher)
        self.imports = LovedImports(repository, catalog, library, remote)
        self.tasks = []
        self.wake = asyncio.Event()

    def linked(self, user_id):
        if self.repository.linked(user_id):
            self.wake.set()

    def report(self, user, values):
        prepared, now = prepare_report(self.catalog, values)
        self.repository.record(user, values, prepared, now)
        if values.get("submission", "true") != "false":
            self.wake.set()

    def control(self, user_id, action):
        self.repository.control(user_id, action)
        self.wake.set()

    def snapshot(self, user_id):
        return self.repository.snapshot(user_id)

    async def start(self):
        for user in self.repository.startup():
            self.linked(user["id"])
        self.tasks = [
            asyncio.create_task(self.loop(self.deliver_one)),
            asyncio.create_task(self.loop(self.import_one)),
        ]

    async def loop(self, operation):
        await run(self, operation)

    async def deliver_one(self):
        return await self.delivery.deliver_one()

    async def import_one(self):
        return await self.imports.import_one()

    async def close(self):
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.tasks = []
