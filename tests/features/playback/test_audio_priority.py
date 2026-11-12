import asyncio

import pytest
from support.audio import OTHER, SOURCE, THIRD, FakeAudio, cache_at

from lastbridge.features.administration.domain.audio import AudioSettings
from lastbridge.shared.errors import CatalogError


class GatedAudio(FakeAudio):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Queue()
        self.gates = {}

    async def fetch(self, source, *args):
        self.started.put_nowait(source)
        await self.gates.setdefault(source, asyncio.Event()).wait()
        return await super().fetch(source, *args)


def test_playback_starts_while_background_downloads_wait(tmp_path):
    async def scenario():
        provider = GatedAudio()
        audio = cache_at(tmp_path, provider)
        first = asyncio.create_task(
            audio.acquire(SOURCE, role="download", background=True)
        )
        assert await provider.started.get() == SOURCE
        second = asyncio.create_task(
            audio.acquire(OTHER, role="download", background=True)
        )
        playback = asyncio.create_task(audio.acquire(THIRD))
        assert await provider.started.get() == THIRD
        assert provider.started.empty() and not second.done()
        assert audio.storage.reserved == 32768
        provider.gates[THIRD].set()
        key, _ = await playback
        await audio.release(key)
        assert provider.started.empty()
        provider.gates[SOURCE].set()
        key, _ = await first
        await audio.release(key)
        assert await provider.started.get() == OTHER
        provider.gates[OTHER].set()
        key, _ = await second
        await audio.release(key)
        assert not audio.storage.reserved and not audio.storage.leases
        await audio.close()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_foreground_promotes_and_shares_queued_prefetch(tmp_path):
    async def scenario():
        provider = GatedAudio()
        audio = cache_at(tmp_path, provider)
        background = asyncio.create_task(
            audio.acquire(SOURCE, background=True)
        )
        assert await provider.started.get() == SOURCE
        prefetch = asyncio.create_task(audio.acquire(OTHER, prefetch=True))
        await asyncio.sleep(0)
        playback = asyncio.create_task(audio.acquire(OTHER))
        assert await provider.started.get() == OTHER
        provider.gates[OTHER].set()
        results = await asyncio.gather(prefetch, playback)
        assert results[0][0] == results[1][0]
        assert provider.calls == [OTHER]
        for key, _ in results:
            await audio.release(key)
        provider.gates[SOURCE].set()
        key, _ = await background
        await audio.release(key)
        await audio.close()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_foreground_waits_for_capacity_and_passes_queued_download(tmp_path):
    async def scenario():
        provider = GatedAudio()
        audio = cache_at(tmp_path, provider)
        first = asyncio.create_task(audio.acquire(SOURCE))
        assert await provider.started.get() == SOURCE
        second = asyncio.create_task(audio.acquire(OTHER))
        assert await provider.started.get() == OTHER
        background = asyncio.create_task(
            audio.acquire("fourthsong1", background=True)
        )
        third = asyncio.create_task(audio.acquire(THIRD))
        await asyncio.sleep(0)
        assert not third.done()
        provider.gates[SOURCE].set()
        key, _ = await first
        await audio.release(key)
        assert await provider.started.get() == THIRD
        assert provider.started.empty()
        # Shutdown wakes pending callers and cleans reservations and leases.
        await audio.close()
        results = await asyncio.gather(
            second, background, third, return_exceptions=True
        )
        assert all(isinstance(r, asyncio.CancelledError) for r in results)
        assert not audio.storage.reserved and not audio.storage.leases
        assert not audio.storage.jobs and not audio.scheduler.waiters

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_queue_bound_and_configuration_guard(tmp_path):
    async def scenario():
        provider = GatedAudio()
        audio = cache_at(tmp_path, provider)
        tasks = [asyncio.create_task(audio.acquire(SOURCE))]
        await provider.started.get()
        tasks.extend(
            asyncio.create_task(audio.acquire(f"{i:011d}")) for i in range(63)
        )
        await asyncio.sleep(0)
        with pytest.raises(CatalogError, match="provider busy"):
            await audio.acquire(OTHER)
        with pytest.raises(ValueError, match="settings busy"):
            await audio.configure(AudioSettings(policy="opus"))
        assert audio.storage.policy == "aac"
        await audio.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert not audio.storage.reserved and not audio.storage.leases

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_cancelled_queued_job_can_be_requested_again(tmp_path):
    async def scenario():
        provider = GatedAudio()
        audio = cache_at(tmp_path, provider)
        first = asyncio.create_task(audio.acquire(SOURCE, background=True))
        assert await provider.started.get() == SOURCE
        queued = asyncio.create_task(audio.acquire(OTHER, prefetch=True))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        replacement = asyncio.create_task(audio.acquire(OTHER))
        assert await provider.started.get() == OTHER
        provider.gates[OTHER].set()
        key, _ = await replacement
        await audio.release(key)
        provider.gates[SOURCE].set()
        key, _ = await first
        await audio.release(key)
        assert provider.calls.count(OTHER) == 1
        assert not audio.storage.reserved and not audio.storage.leases
        await audio.close()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_stream_promotes_queued_download_with_stream_provider(tmp_path):
    class Router(GatedAudio):
        def __init__(self):
            super().__init__()
            self.roles = []

        def for_role(self, role):
            self.roles.append(role)
            return self

    async def scenario():
        provider = Router()
        audio = cache_at(tmp_path, provider)
        first = asyncio.create_task(
            audio.acquire(SOURCE, role="download", background=True)
        )
        assert await provider.started.get() == SOURCE
        queued = asyncio.create_task(
            audio.acquire(OTHER, role="download", background=True)
        )
        await asyncio.sleep(0)
        playback = asyncio.create_task(audio.acquire(OTHER))
        assert await provider.started.get() == OTHER
        assert provider.roles == ["download", "stream"]
        provider.gates[OTHER].set()
        for key, _ in await asyncio.gather(queued, playback):
            await audio.release(key)
        provider.gates[SOURCE].set()
        key, _ = await first
        await audio.release(key)
        await audio.close()

    asyncio.run(asyncio.wait_for(scenario(), 5))
