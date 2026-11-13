import asyncio
import logging

from lastbridge.features.discovery.application.builder import MixBuilder
from lastbridge.features.discovery.application.lists import MixLists
from lastbridge.features.discovery.application.ports import (
    CatalogAccess,
    LibraryAccess,
    MixStore,
    TasteProvider,
)
from lastbridge.features.discovery.application.worker import run
from lastbridge.features.discovery.domain.policy import DAY, DEADLINE
from lastbridge.shared.errors import CatalogError, ProviderError
from lastbridge.shared.lastfm import LastfmRequestError

logger = logging.getLogger(__name__)


class Mixes:
    def __init__(
        self,
        repository: MixStore,
        catalog: CatalogAccess,
        library: LibraryAccess,
        remote: TasteProvider,
        *,
        refresh_interval=DAY,
        inactive_ttl=7 * DAY,
    ):
        self.repository, self.catalog, self.library, self.remote = (
            repository,
            catalog,
            library,
            remote,
        )
        self.refresh_interval, self.inactive_ttl = (
            refresh_interval,
            inactive_ttl,
        )
        self.task = None
        self.wake = asyncio.Event()
        self.builder = MixBuilder(repository, catalog, remote)
        self.lists = MixLists(repository, catalog, library)

    def linked(self, user_id):
        if self.repository.linked(user_id):
            self.wake.set()

    def touch(self, user_id):
        if self.repository.touch(user_id):
            self.wake.set()

    def request(self, user_id):
        self.linked(user_id)
        self.touch(user_id)
        self.repository.request(user_id)
        self.wake.set()

    def status(self, user_id):
        return self.repository.status(user_id)

    def user(self, user_id):
        return self.repository.user(user_id)

    def local(self, user_id, target):
        return self.repository.local(user_id, target)

    def playlists(self, user, item_id=None):
        return self.lists.playlists(user, item_id)

    async def start(self):
        for user_id in self.repository.startup(self.refresh_interval):
            self.linked(user_id)
        self.task = asyncio.create_task(self.worker())

    async def worker(self):
        await run(self)

    async def build(self, job):
        return await self.builder.build(job)

    async def refresh_one(self):
        if not self.remote.configured:
            return False
        job = self.repository.claim(self.inactive_ttl)
        if job is None:
            return False
        if not job:
            return True
        try:
            async with asyncio.timeout(DEADLINE):
                profile, snapshot = await self.build(job)
        except (
            LastfmRequestError,
            CatalogError,
            ProviderError,
            TimeoutError,
        ) as failure:
            logger.warning(
                "Mix refresh failed user_id=%s error_type=%s code=%s; "
                "previous playlists kept, retry in five minutes",
                job["user_id"],
                type(failure).__name__,
                getattr(failure, "code", None),
            )
            self.repository.failed(job)
            return True
        self.repository.publish(job, profile, snapshot, self.refresh_interval)
        return True

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
