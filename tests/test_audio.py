import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_catalog
from test_catalog import body, get, search

from lastbridge.audio import AudioCache, AudioProvider
from lastbridge.catalog import CatalogError
from lastbridge.providers.audio_worker import choose_format, download
from lastbridge.providers.base import ProviderError

catalog_service = test_catalog.catalog_service

SOURCE = "khnokW3Mw24"
OTHER = "HYXGY67fATk"
THIRD = "d8CbKd69KT8"
AUDIO = bytes(range(256)) * 32


class FakeAudio:
    def __init__(self, data=AUDIO):
        self.data = data
        self.calls = []
        self.fail = False

    async def fetch(self, source, policy, directory, maximum, reserve):
        self.calls.append(source)
        extension = "m4a" if policy == "aac" else "webm"
        path = directory / f"audio.{extension}"
        path.write_bytes(self.data)
        if self.fail:
            raise CatalogError(0, "Audio provider unavailable; retry")
        return {"policy": policy}


def cache_at(tmp_path, provider=None, **kwargs):
    return AudioCache(
        tmp_path / "audio",
        budget=32_768,
        maximum=8192,
        reserve=0,
        provider=provider or FakeAudio(),
        **kwargs,
    )


def test_authenticated_stream_download_ranges_and_head(catalog_service):
    client, app, _, keys = catalog_service
    fake = app.state.audio.provider = FakeAudio()
    song = search(catalog_service)["song"][0]
    params = {
        "apiKey": keys["admin"],
        "v": "1.16.1",
        "c": "test",
        "id": song["id"],
    }
    response = client.get("/rest/stream.view", params=params)
    assert response.status_code == 200
    assert response.content == AUDIO
    assert response.headers["content-type"] == "audio/mp4"
    assert response.headers["content-length"] == str(len(AUDIO))
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-disposition"].startswith("inline;")
    etag = response.headers["etag"]
    for header, expected in (
        ("bytes=100-199", AUDIO[100:200]),
        ("bytes=8100-", AUDIO[8100:]),
        ("bytes=-50", AUDIO[-50:]),
    ):
        response = client.get(
            "/rest/stream", params=params, headers={"Range": header}
        )
        assert response.status_code == 206
        assert response.content == expected
        assert response.headers["content-length"] == str(len(expected))
    response = client.get(
        "/rest/stream", params=params, headers={"Range": "bytes=8192-"}
    )
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */8192"
    response = client.get(
        "/rest/stream", params=params, headers={"Range": "broken"}
    )
    assert response.status_code == 400
    response = client.get(
        "/rest/stream", params=params, headers={"Range": "bytes=0-9,100-109"}
    )
    assert response.status_code == 206
    assert "multipart/byteranges" in response.headers["content-type"]
    assert (
        AUDIO[:10] in response.content and AUDIO[100:110] in response.content
    )
    response = client.get(
        "/rest/stream",
        params=params,
        headers={"Range": "bytes=0-9", "If-Range": etag},
    )
    assert response.status_code == 206
    response = client.get(
        "/rest/stream",
        params=params,
        headers={"Range": "bytes=0-9", "If-Range": '"old"'},
    )
    assert response.status_code == 200 and response.content == AUDIO
    response = client.head("/rest/stream.view", params=params)
    assert response.status_code == 200 and response.content == b""
    assert response.headers["content-length"] == str(len(AUDIO))
    response = client.post("/rest/download.view", data=params)
    assert response.content == AUDIO
    assert response.headers["content-disposition"].startswith("attachment;")
    # Another linked user's request reuses the same public recording bytes.
    response = client.get(
        "/rest/stream", params=params | {"apiKey": keys["alice"]}
    )
    assert response.content == AUDIO
    assert fake.calls == [SOURCE]
    assert not app.state.audio.leases
    assert (
        body(get(catalog_service, "search3", query=""))["searchResult3"][
            "song"
        ]
        == []
    )


@pytest.mark.parametrize("method", ["stream", "download"])
def test_audio_rejects_auth_unlinked_and_wrong_catalog_ids(
    catalog_service, method
):
    client, app, _, keys = catalog_service
    fake = app.state.audio.provider = FakeAudio()
    results = search(catalog_service)
    song = results["song"][0]["id"]
    base = {"v": "1.16.1", "c": "test", "f": "json", "id": song}
    for credentials, code in (
        ({}, 40),
        ({"apiKey": "wrong"}, 44),
        ({"apiKey": keys["unlinked"]}, 50),
    ):
        response = client.get("/rest/" + method, params=base | credentials)
        assert body(response)["error"]["code"] == code
    for item in (
        "../credentials.key",
        results["album"][0]["id"],
        "s" + "0" * 32,
    ):
        assert (
            body(get(catalog_service, method, id=item))["error"]["code"] == 70
        )
    assert fake.calls == []


def test_audio_failure_retry_and_parameters(catalog_service):
    _, app, _, _ = catalog_service
    fake = app.state.audio.provider = FakeAudio()
    song = search(catalog_service)["song"][0]["id"]
    fake.fail = True
    assert body(get(catalog_service, "stream", id=song))["error"]["code"] == 0
    assert not app.state.audio.entries and not app.state.audio.leases
    assert list(app.state.audio.incoming.iterdir()) == []
    fake.fail = False
    assert (
        body(get(catalog_service, "stream", id=song, timeOffset="10"))[
            "error"
        ]["code"]
        == 0
    )
    assert (
        body(get(catalog_service, "stream", id=song, maxBitRate="-1"))[
            "error"
        ]["code"]
        == 10
    )
    response = get(
        catalog_service, "stream", id=song, format="mp3", maxBitRate="320"
    )
    assert (
        response.content == AUDIO
    )  # Preferred transcode parameters cannot upscale source.
    assert len(fake.calls) == 2


def test_cache_lru_active_leases_restart_and_missing_data(tmp_path):
    async def scenario():
        fake = FakeAudio()
        cache = cache_at(tmp_path, fake)
        first, _ = await cache.acquire(SOURCE)
        second, _ = await cache.acquire(OTHER)
        await cache.release(second)
        third, _ = await cache.acquire(THIRD)
        await cache.release(third)
        # Reduce the three-file budget; the first file is still active.
        cache.budget = 16_384
        cache.trim(0)
        assert first in cache.entries and second not in cache.entries
        await cache.release(first)
        await cache.close()
        incoming = tmp_path / "audio" / ".incoming" / "interrupted"
        incoming.mkdir()
        (incoming / "audio.m4a.part").write_bytes(b"partial")
        orphan = tmp_path / "audio" / f"{OTHER}-aac.m4a"
        orphan.write_bytes(b"uncommitted")
        cache = cache_at(tmp_path, fake)
        assert list(cache.incoming.iterdir()) == [] and not orphan.exists()
        key, _ = await cache.acquire(SOURCE)
        await cache.release(key)
        assert fake.calls.count(SOURCE) == 1
        (cache.directory / f"{key}.m4a").unlink()
        key, _ = await cache.acquire(SOURCE)
        await cache.release(key)
        assert fake.calls.count(SOURCE) == 2
        await cache.close()

    asyncio.run(scenario())


def test_concurrent_waiters_share_job_and_cancelled_waiter_does_not_cancel_it(
    tmp_path,
):
    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()

        class Delayed(FakeAudio):
            async def fetch(self, *args):
                started.set()
                await finish.wait()
                return await super().fetch(*args)

        fake = Delayed()
        cache = cache_at(tmp_path, fake)
        first = asyncio.create_task(cache.acquire(SOURCE))
        await started.wait()
        second = asyncio.create_task(cache.acquire(SOURCE))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert len(cache.jobs) == 1 and cache.leases[f"{SOURCE}-aac"] == 1
        finish.set()
        key, _ = await second
        assert fake.calls == [SOURCE]
        await cache.release(key)
        assert not cache.leases and cache.reserved == 0
        await cache.close()

    asyncio.run(scenario())


def test_busy_cache_protects_active_files_and_limits_jobs(tmp_path):
    async def scenario():
        finish = asyncio.Event()

        class Delayed(FakeAudio):
            async def fetch(self, *args):
                await finish.wait()
                return await super().fetch(*args)

        cache = cache_at(tmp_path, Delayed())
        tasks = [
            asyncio.create_task(cache.acquire(s)) for s in (SOURCE, OTHER)
        ]
        await asyncio.sleep(0)
        assert cache.reserved == 32_768
        with pytest.raises(CatalogError, match="provider busy"):
            await cache.acquire(THIRD)
        finish.set()
        entries = await asyncio.gather(*tasks)
        cache.budget = 16_384
        with pytest.raises(CatalogError, match="cache busy"):
            await cache.acquire(THIRD)
        assert len(cache.entries) == 2
        for key, _ in entries:
            await cache.release(key)
        await cache.close()

    asyncio.run(scenario())


def test_size_disk_and_shutdown_failures_leave_no_committed_file(
    tmp_path, monkeypatch
):
    async def scenario():
        fake = FakeAudio(b"x" * 8193)
        cache = cache_at(tmp_path, fake)
        with pytest.raises(CatalogError, match="Invalid audio"):
            await cache.acquire(SOURCE)
        assert not cache.entries and not cache.jobs and not cache.reserved
        assert not cache.leases and list(cache.incoming.iterdir()) == []
        monkeypatch.setattr(
            "lastbridge.audio.shutil.disk_usage",
            lambda _: SimpleNamespace(free=0),
        )
        with pytest.raises(CatalogError, match="free space"):
            await cache.acquire(SOURCE)
        assert fake.calls == [SOURCE]
        await cache.close()
        with pytest.raises(CatalogError, match="shutting down"):
            await cache.acquire(SOURCE)

    asyncio.run(scenario())


def test_storage_single_owner_and_malformed_manifest_recovery(tmp_path):
    async def scenario():
        cache = cache_at(tmp_path)
        with pytest.raises(RuntimeError, match="already in use"):
            cache_at(tmp_path)
        await cache.close()
        directory = tmp_path / "audio"
        (directory / f"{SOURCE}-aac.json").write_text(
            '{"policy":"aac","size":42}'
        )
        (directory / f"{SOURCE}-aac.m4a").write_bytes(b"bad")
        cache = cache_at(tmp_path)
        assert not cache.entries
        assert not (directory / f"{SOURCE}-aac.m4a").exists()
        await cache.close()

    asyncio.run(scenario())


def test_response_disconnect_releases_lease(tmp_path):
    async def scenario():
        cache = cache_at(tmp_path)
        response = await cache.response(SOURCE)

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        async def receive():
            return {"type": "http.disconnect"}

        with pytest.raises(OSError):
            await response(
                {
                    "type": "http",
                    "method": "GET",
                    "headers": [],
                    "asgi": {"spec_version": "2.4"},
                },
                receive,
                send,
            )
        assert not cache.leases
        await cache.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("behavior", ["timeout", "oversized", "unsafe_error"])
def test_audio_worker_is_bounded_and_errors_are_redacted(
    tmp_path, monkeypatch, behavior
):
    async def scenario():
        original = asyncio.create_subprocess_exec
        processes = []
        script = {
            "timeout": "import sys,time; sys.stdin.read(); time.sleep(60)",
            "oversized": "import sys; sys.stdin.read(); print('x'*70000)",
            "unsafe_error": (
                "import sys; sys.stdin.read(); "
                'print(\'{"error":"https://secret.invalid/credential"}\')'
            ),
        }[behavior]

        async def launch(*args, **kwargs):
            process = await original(sys.executable, "-c", script, **kwargs)
            processes.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
        with pytest.raises(CatalogError) as error:
            await AudioProvider(
                timeout=0.1 if behavior == "timeout" else 3
            ).fetch(SOURCE, "aac", tmp_path, 8192, 0)
        assert "secret" not in str(error.value)
        assert processes[0].returncode is not None

    asyncio.run(scenario())


def test_source_format_policy_does_not_compare_bitrates_across_codecs():
    aac = {"ext": "m4a", "acodec": "mp4a.40.2", "abr": 129}
    opus = {"ext": "webm", "acodec": "opus", "abr": 145}
    assert choose_format([aac, opus], "aac") == aac
    assert choose_format([aac, opus], "opus") == opus
    with pytest.raises(ProviderError, match="no_audio_format"):
        choose_format([opus], "aac")


def test_worker_full_track_download_and_validation(tmp_path, monkeypatch):
    options = {}

    class Downloader:
        def __init__(self, opts):
            options.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download):
            assert download is True and url.endswith(SOURCE)
            selected = list(
                options["format"](
                    {
                        "formats": [
                            {
                                "ext": "m4a",
                                "acodec": "mp4a.40.2",
                                "vcodec": "none",
                                "protocol": "https",
                                "url": "https://example.invalid/audio",
                                "abr": 129,
                            }
                        ]
                    }
                )
            )
            assert selected[0]["ext"] == "m4a"
            Path(options["outtmpl"].replace("%(ext)s", "m4a")).write_bytes(
                AUDIO
            )
            return {"duration": 337}

    monkeypatch.setattr("yt_dlp.YoutubeDL", Downloader)

    def probe(args, **kwargs):
        assert "-t" not in args
        if args[0] == "ffmpeg":
            assert "-xerror" in args
            return SimpleNamespace(returncode=0)
        assert args[0] == "ffprobe"
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "streams": [{"codec_type": "audio", "codec_name": "aac"}],
                    "format": {"duration": "337.01"},
                }
            )
        )

    monkeypatch.setattr(
        "lastbridge.providers.audio_worker.subprocess.run", probe
    )
    payload = {
        "source": SOURCE,
        "policy": "aac",
        "directory": str(tmp_path),
        "maximum": 8192,
        "reserve": 0,
    }
    assert download(payload)["policy"] == "aac"
    assert options["continuedl"] is False
    assert options["remote_components"] == []
    with pytest.raises(ProviderError, match="too_large"):
        options["progress_hooks"][0]({"downloaded_bytes": 8193})


def test_shutdown_cancels_acquisition_and_discards_staging(tmp_path):
    async def scenario():
        started = asyncio.Event()

        class Interrupted(FakeAudio):
            async def fetch(self, source, policy, directory, *limits):
                (directory / "audio.m4a.part").write_bytes(b"partial")
                started.set()
                await asyncio.Event().wait()

        cache = cache_at(tmp_path, Interrupted())
        request = asyncio.create_task(cache.acquire(SOURCE))
        await started.wait()
        await cache.close()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not cache.entries and not cache.jobs
        assert cache.reserved == 0 and not cache.leases
        assert list(cache.incoming.iterdir()) == []
        # Lock is released; next startup can recover immediately.
        replacement = cache_at(tmp_path)
        await replacement.close()

    asyncio.run(scenario())


def test_manifest_write_failure_does_not_publish_audio(tmp_path, monkeypatch):
    async def scenario():
        cache = cache_at(tmp_path)

        def fail(*args):
            raise OSError("disk full")

        monkeypatch.setattr(cache, "manifest", fail)
        with pytest.raises(CatalogError, match="acquisition failed"):
            await cache.acquire(SOURCE)
        assert not cache.entries and not cache.leases and not cache.reserved
        assert list(cache.directory.glob("*.m4a")) == []
        await cache.close()

    asyncio.run(scenario())


def test_opus_policy_and_original_source_bytes(catalog_service):
    client, app, _, keys = catalog_service
    fake = app.state.audio.provider = FakeAudio()
    app.state.audio.policy = "opus"
    song = search(catalog_service)["song"][0]["id"]
    response = get(catalog_service, "stream", id=song)
    assert response.content == AUDIO
    assert response.headers["content-type"] == "audio/webm"
    assert response.headers["content-disposition"].endswith('.webm"')
    assert fake.calls == [SOURCE]
