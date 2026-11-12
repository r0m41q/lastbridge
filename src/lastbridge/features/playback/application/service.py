import asyncio
import contextlib
import logging
import time

import anyio

from lastbridge.features.playback.application.ports import (
    AudioConfiguration,
    AudioProvider,
    AudioStorage,
    ReadyNotifier,
    ResponseFactory,
)
from lastbridge.features.playback.application.scheduling import AudioScheduler
from lastbridge.features.playback.domain.models import VIDEO_ID
from lastbridge.shared.errors import CatalogError

logger = logging.getLogger(__name__)


class AudioCache:
    """Coordinate bounded acquisition and leases over injected storage."""

    def __init__(
        self,
        storage: AudioStorage,
        provider: AudioProvider,
        response_factory: ResponseFactory,
        ready_notifier: ReadyNotifier | None = None,
    ):
        self.storage = storage
        self.provider = provider
        self.response_factory = response_factory
        self.ready_notifier = ready_notifier
        self.scheduler = AudioScheduler()
        self.changed = asyncio.Event()
        self.wait_timeout = 30
        self.roles = {}

    async def configure(self, settings: AudioConfiguration):
        if settings.stream_provider not in ("youtube", "jumo", "rhythmax") or (
            settings.download_provider not in ("youtube", "jumo", "rhythmax")
        ):
            raise ValueError("Select a supported audio provider.")
        configure = getattr(self.provider, "configure", None)
        async with self.storage.lock:
            if self.storage.jobs:
                raise ValueError("Audio settings busy; retry after downloads")
            if configure is not None:
                await configure(settings)
            self.storage.budget = settings.cache_bytes
            self.storage.maximum = min(
                settings.file_max_bytes, settings.cache_bytes // 2
            )
            self.storage.allowance = 2 * self.storage.maximum
            self.storage.reserve = settings.free_reserve_bytes
            self.storage.policy = settings.policy
            if configure is None and hasattr(self.provider, "timeout"):
                self.provider.timeout = settings.timeout_seconds

    @property
    def entries(self):
        return self.storage.entries

    def path(self, key, entry):
        return self.storage.path(key, entry)

    def manifest(self, key, entry):
        self.storage.manifest(key, entry)

    async def update_pins(self, keys):
        # Set synchronously before the first await: library transactions and
        # cache eviction run on this event loop and cannot see a stale pin set.
        self.storage.pins = set(keys)
        async with self.storage.lock:
            for key, entry in list(self.storage.entries.items()):
                if (
                    entry.get("saved")
                    and entry.get("policy") == self.storage.policy
                    and key not in self.storage.pins
                    and not self.storage.leases[key]
                ):
                    with contextlib.suppress(OSError):
                        self.storage.relocate(key, False)
            self.storage.trim(0)

    async def retain(self, key):
        async with self.storage.lock:
            if key not in self.storage.pins:
                return False
            self.storage.relocate(key, True)
        # A liked track can already be cached from streaming, so fill() may
        # not run. Wake cover population for promotions as well as downloads.
        if self.ready_notifier is not None:
            await self.ready_notifier.notify()
        return True

    async def acquire(
        self, source, role="stream", *, background=False, prefetch=False
    ):
        if not VIDEO_ID.fullmatch(source):
            raise CatalogError(70, "Recording not found")
        key = f"{source}-{self.storage.policy}"
        priority = 2 if prefetch else 1 if background else 0
        try:
            async with self.storage.lock:
                if self.storage.closed:
                    raise CatalogError(0, "Audio cache is shutting down")
                entry = self.storage.cached_entry(key)
                if not entry and key not in self.storage.jobs:
                    if len(self.storage.jobs) >= 64:
                        raise CatalogError(
                            0, "Audio provider busy; retry shortly"
                        )
                    self.scheduler.submit(key, priority)
                    self.roles[key] = role
                    task = asyncio.create_task(self.fill(source, key))
                    self.storage.jobs[key] = task
                    # Retrieve errors even when all requesting clients cancel.
                    task.add_done_callback(
                        lambda t: None if t.cancelled() else t.exception()
                    )
                elif not entry:
                    if role == "stream":
                        self.roles[key] = "stream"
                    self.scheduler.promote(key, priority)
                self.storage.leases[key] += 1
                job = self.storage.jobs.get(key)
            try:
                if job:
                    await asyncio.shield(job)
                async with self.storage.lock:
                    entry = self.storage.entries[key]
                    entry["used"] = time.time()
                    # This file already has a commit marker. A full disk must
                    # not prevent playback just because its LRU touch failed.
                    with contextlib.suppress(OSError):
                        self.manifest(key, entry)
                return key, dict(entry)
            except BaseException as error:
                if not isinstance(error, asyncio.CancelledError):
                    logger.warning(
                        "Audio acquisition failed role=%s source=%s "
                        "error_type=%s reason=%s",
                        role,
                        source,
                        type(error).__name__,
                        getattr(error, "message", "unknown"),
                    )
                with anyio.CancelScope(shield=True):
                    await self.release(key)
                raise
        except OSError:
            raise CatalogError(0, "Audio storage unavailable") from None

    async def admit(self, key):
        try:
            async with asyncio.timeout(self.wait_timeout):
                await self.scheduler.wait(key)
                while True:
                    async with self.storage.lock:
                        if self.storage.closed:
                            raise CatalogError(
                                0, "Audio cache is shutting down"
                            )
                        self.changed.clear()
                        try:
                            self.storage.trim(self.storage.allowance)
                        except CatalogError as error:
                            if (
                                error.message
                                != "Audio cache busy; retry shortly"
                            ):
                                raise
                        else:
                            self.storage.check_space()
                            allowance = self.storage.allowance
                            self.storage.reserved += allowance
                            return allowance
                    await self.changed.wait()
        except TimeoutError:
            raise CatalogError(
                0, "Audio acquisition queue timed out; retry"
            ) from None

    async def fill(self, source, key):
        destination = None
        saved = False
        allowance = 0
        try:
            allowance = await self.admit(key)
            with self.storage.staging() as directory:
                provider = getattr(self.provider, "for_role", lambda _: None)(
                    self.roles[key]
                )
                if provider is None:
                    provider = self.provider
                result = await provider.fetch(
                    source,
                    self.storage.policy,
                    directory,
                    self.storage.maximum,
                    self.storage.reserve,
                )
                entry = self.storage.validate(directory, result)
                async with self.storage.lock:
                    entry["saved"] = key in self.storage.pins
                    destination = self.storage.publish(key, directory, entry)
                    saved = entry["saved"]
                if saved and self.ready_notifier is not None:
                    await self.ready_notifier.notify()
        except OSError, ValueError, TypeError, KeyError:
            raise CatalogError(0, "Audio acquisition failed; retry") from None
        finally:
            async with self.storage.lock:
                if destination is not None and key not in self.storage.entries:
                    with contextlib.suppress(OSError):
                        self.storage.discard(destination)
                self.storage.reserved -= allowance
                self.changed.set()
                if self.storage.jobs.get(key) is asyncio.current_task():
                    self.storage.jobs.pop(key, None)
                    self.roles.pop(key, None)
                    self.scheduler.finish(key)

    async def response(self, source, download=False):
        role = "download" if download else "stream"
        key, entry = await self.acquire(source, role=role)
        try:
            return self.response_factory(self, key, entry, download)
        except BaseException:
            await self.release(key)
            raise

    async def release(self, key):
        async with self.storage.lock:
            self.storage.leases[key] -= 1
            if not self.storage.leases[key]:
                del self.storage.leases[key]
                self.changed.set()
                if key in self.scheduler.waiters:
                    self.storage.jobs.pop(key).cancel()
                    self.roles.pop(key, None)
                    self.scheduler.finish(key)
                entry = self.storage.entries.get(key)
                if (
                    entry
                    and entry.get("saved")
                    and key not in self.storage.pins
                ):
                    with contextlib.suppress(OSError):
                        self.storage.relocate(key, False)
                        self.storage.trim(0)
                with contextlib.suppress(OSError):
                    self.storage.cleanup_aliases(key)

    async def close(self):
        self.storage.closed = True
        self.scheduler.closed = True
        jobs = list(self.storage.jobs.values())
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self.storage.jobs.clear()
        self.scheduler.waiters.clear()
        self.scheduler.active.clear()
        self.roles.clear()
        self.storage.close()
        if self.ready_notifier is not None:
            await self.ready_notifier.close()
