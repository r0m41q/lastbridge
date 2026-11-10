import asyncio
from pathlib import Path

import pytest
from support.artwork import PNG, make_saved_artwork, save_record
from support.audio import OTHER, SOURCE

from lastbridge.features.playback.adapters.filesystem.artwork import (
    COVER_NAMES,
)


def test_backfills_one_cover_per_album_without_touching_audio(tmp_path):
    async def scenario():
        scans = []

        class Notifier:
            async def notify(self):
                assert cover.read_bytes() == PNG
                scans.append(True)

            async def close(self):
                pass

        service, repository, covers = make_saved_artwork(
            tmp_path, notifier=Notifier()
        )
        first = save_record(repository, covers.root)
        second = save_record(
            repository,
            covers.root,
            OTHER,
            "Artist/Album/02 - Track [opus].webm",
            "opus",
        )
        cover = covers.root / "Artist/Album/cover.png"
        original = {
            record.relative_path: (
                covers.root / record.relative_path
            ).read_bytes()
            for record in (first, second)
        }
        assert await service.populate()
        assert len(service.source.calls) == 1
        assert scans == [True]
        assert not await service.populate()
        assert len(service.source.calls) == 1 and scans == [True]
        for record in (first, second):
            assert (covers.root / record.relative_path).read_bytes() == (
                original[record.relative_path]
            )
            assert repository.get(record.source, record.policy) == record
        assert not list(cover.parent.glob(".lastbridge-cover-*"))
        await service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("name", (*COVER_NAMES, "Cover.JPG", "Folder.PNG"))
def test_preserves_existing_user_cover(tmp_path, name):
    service, repository, covers = make_saved_artwork(tmp_path)
    record = save_record(repository, covers.root)
    manual = (covers.root / record.relative_path).parent / name
    manual.write_bytes(b"user selected artwork")
    assert not asyncio.run(service.populate())
    assert manual.read_bytes() == b"user selected artwork"
    assert service.source.calls == []


def test_missing_or_unfinished_audio_is_not_populated(tmp_path):
    service, repository, covers = make_saved_artwork(tmp_path)
    record = save_record(repository, covers.root)
    (covers.root / record.relative_path).unlink()
    save_record(
        repository,
        covers.root,
        OTHER,
        "Artist/Other/Track.m4a",
        state="writing",
    )
    assert not asyncio.run(service.populate())
    assert not service.source.calls


def test_missing_track_does_not_hide_another_saved_track_in_same_album(
    tmp_path,
):
    service, repository, covers = make_saved_artwork(tmp_path)
    save_record(repository, covers.root)
    save_record(repository, covers.root, OTHER, "Artist/Album/Other.m4a")
    first = repository.list()[0]
    (covers.root / first.relative_path).unlink()
    assert asyncio.run(service.populate())
    assert len(service.source.calls) == 1
    assert (covers.root / "Artist/Album/cover.png").read_bytes() == PNG


def test_failure_retries_and_does_not_fail_saved_audio(tmp_path):
    service, repository, covers = make_saved_artwork(tmp_path)
    record = save_record(repository, covers.root)
    service.source.fail_sources.add(SOURCE)
    assert not asyncio.run(service.populate())
    assert repository.get(SOURCE, "aac") == record
    service.source.fail_sources.clear()
    assert asyncio.run(service.populate())
    assert service.source.calls == [SOURCE, SOURCE]


def test_another_track_can_supply_missing_album_art(tmp_path):
    service, repository, covers = make_saved_artwork(tmp_path)
    save_record(repository, covers.root)
    save_record(repository, covers.root, OTHER, "Artist/Album/02 - Other.m4a")
    # Repository order is stable; neither source is assumed to be first.
    first_source = repository.list()[0].source
    service.source.fail_sources.add(first_source)
    assert asyncio.run(service.populate())
    assert len(service.source.calls) == 2


def test_track_removed_during_fetch_is_not_populated(tmp_path):
    async def scenario():
        class RemovingSource:
            async def fetch(self, source):
                repository.delete(source, "aac")
                return PNG, "image/png"

        service, repository, covers = make_saved_artwork(
            tmp_path, source=RemovingSource()
        )
        save_record(repository, covers.root)
        assert not await service.populate()
        assert not (covers.root / "Artist/Album/cover.png").exists()

    asyncio.run(scenario())


def test_background_start_and_notify_populate_new_saves(tmp_path):
    async def scenario():
        service, repository, covers = make_saved_artwork(tmp_path)
        service.start()
        try:
            await asyncio.sleep(0)
            assert not service.source.calls
            save_record(repository, covers.root)
            await service.notify()
            async with asyncio.timeout(2):
                while not (covers.root / "Artist/Album/cover.png").exists():
                    await asyncio.sleep(0.01)
            assert service.source.calls == [SOURCE]
        finally:
            await service.close()
        assert service.task is None

    asyncio.run(scenario())


def test_shutdown_cancels_inflight_artwork(tmp_path):
    async def scenario():
        started = asyncio.Event()

        class SlowSource:
            async def fetch(self, source):
                started.set()
                await asyncio.Event().wait()

        service, repository, covers = make_saved_artwork(
            tmp_path, source=SlowSource()
        )
        save_record(repository, covers.root)
        service.start()
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(service.close(), 1)
        assert service.task is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "data,media_type",
    [
        (b"<html>error</html>", "image/png"),
        (PNG, "image/jpeg"),
        (PNG, "text/html"),
        (b"", "image/png"),
        (PNG * 40000, "image/png"),
    ],
)
def test_invalid_artwork_is_not_written(tmp_path, data, media_type):
    _, repository, covers = make_saved_artwork(tmp_path)
    record = save_record(repository, covers.root)
    with pytest.raises(ValueError, match="Invalid artwork"):
        covers.write(record.relative_path, data, media_type)
    assert not list((covers.root / "Artist/Album").glob("cover.*"))
    assert not list((covers.root / "Artist/Album").glob(".lastbridge-cover-*"))


def test_cover_publication_never_overwrites_a_concurrent_manual_cover(
    tmp_path, monkeypatch
):
    _, repository, covers = make_saved_artwork(tmp_path)
    record = save_record(repository, covers.root)

    def race(source, destination):
        Path(destination).write_bytes(b"manual cover")
        raise FileExistsError

    monkeypatch.setattr(
        "lastbridge.features.playback.adapters.filesystem.artwork.os.link",
        race,
    )
    assert not covers.write(record.relative_path, PNG, "image/png")
    assert (
        covers.root / "Artist/Album/cover.png"
    ).read_bytes() == b"manual cover"
    assert not list((covers.root / "Artist/Album").glob(".lastbridge-cover-*"))


def test_cover_path_cannot_escape_saved_root(tmp_path):
    _, repository, covers = make_saved_artwork(tmp_path)
    save_record(repository, covers.root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "track.m4a").write_bytes(b"unmanaged")
    (covers.root / "symlink").symlink_to(outside, target_is_directory=True)
    for path in ("../outside/track.m4a", "symlink/track.m4a"):
        with pytest.raises(ValueError, match="Invalid saved audio path"):
            covers.write(path, PNG, "image/png")
    assert not (outside / "cover.png").exists()


def test_production_bootstrap_populates_managed_album_art(tmp_path):
    import httpx
    from fastapi.testclient import TestClient

    from lastbridge.app import create_app
    from lastbridge.config import Settings
    from lastbridge.features.playback.application.artwork import SavedArtwork

    app = create_app(
        Settings(_env_file=None, data_dir=tmp_path / "data"),
        start_workers=False,
    )
    with TestClient(app) as client:
        services = app.state.services
        artwork = services.audio.ready_notifier
        assert isinstance(artwork, SavedArtwork)
        services.catalog.put(
            "song",
            SOURCE,
            {"title": "Bodies", "artist": "Offset, JID", "album": "Bodies"},
            "https://lh3.googleusercontent.com/cover=w60-h60-l90-rj",
        )
        record = save_record(artwork.repository, artwork.covers.root)
        calls = []

        def respond(request):
            calls.append(str(request.url))
            return httpx.Response(
                200, content=PNG, headers={"content-type": "image/png"}
            )

        async def populate():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond)
            ) as http:
                artwork.source.client = http
                assert await artwork.populate()

        client.portal.call(populate)
        assert calls == [
            "https://lh3.googleusercontent.com/cover=w1080-h1080-p-l90-rj"
        ]
        assert (artwork.covers.root / record.relative_path).read_bytes() == (
            b"saved audio fixture"
        )
        assert (
            artwork.covers.root / "Artist/Album/cover.png"
        ).read_bytes() == PNG
