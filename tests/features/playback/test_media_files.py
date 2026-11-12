import asyncio
import shutil
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from support.audio import SOURCE, FakeAudio

from lastbridge.features.playback.adapters.filesystem.metadata import (
    MetadataWriter,
)
from lastbridge.features.playback.adapters.filesystem.storage import AudioFiles
from lastbridge.features.playback.adapters.http.response import AudioResponse
from lastbridge.features.playback.adapters.jellyfin.notifier import (
    JellyfinScanNotifier,
)
from lastbridge.features.playback.adapters.sqlite.repository import (
    MediaFileRepository,
)
from lastbridge.features.playback.application.media import MediaPlacement
from lastbridge.features.playback.application.service import AudioCache
from lastbridge.features.playback.domain.models import (
    MediaDescriptor,
    MediaFile,
)
from lastbridge.features.playback.domain.paths import saved_relative_path
from lastbridge.platform.credentials import Credentials
from lastbridge.platform.database import Database
from lastbridge.platform.migrations import migrate
from lastbridge.shared.errors import CatalogError


def make_repository(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir(exist_ok=True)
    credentials = Credentials(directory, directory / "lastbridge.sqlite3")
    database = Database(directory)
    migrate(database, credentials)
    return MediaFileRepository(database), database


def make_managed_audio(
    tmp_path, pins=(), metadata_writer=None, ready_notifier=None
):
    repository, _ = make_repository(tmp_path)
    descriptors = {
        SOURCE: MediaDescriptor(
            source=SOURCE,
            policy="aac",
            title="Song",
            artist="Artist",
            album="Album",
            track_number=1,
        )
    }

    class CopyMetadata:
        def write(self, source, destination, descriptor):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            return destination

    def descriptor_factory(source, policy):
        return replace(descriptors[source], policy=policy)

    storage = AudioFiles(
        tmp_path / "cache" / "audio",
        budget=32768,
        maximum=8192,
        reserve=0,
        policy="aac",
        saved_directory=tmp_path / "saved" / "audio",
        pins=pins,
        media_files=repository,
        media_placement=MediaPlacement(repository),
        metadata_writer=metadata_writer or CopyMetadata(),
        descriptor_factory=descriptor_factory,
    )
    return (
        AudioCache(
            storage,
            FakeAudio(),
            AudioResponse,
            ready_notifier,
        ),
        repository,
    )


def test_media_migration_creates_version_13_table(tmp_path):
    repository, database = make_repository(tmp_path)
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 22
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(media_files)")
        }
    assert columns == {
        "source",
        "policy",
        "relative_path",
        "container",
        "size",
        "checksum",
        "provider_identity",
        "state",
        "created",
        "updated",
    }
    assert repository.list() == []


def test_media_repository_keeps_multiple_policies_for_one_source(tmp_path):
    repository, _ = make_repository(tmp_path)
    descriptor = MediaDescriptor(
        source="khnokW3Mw24",
        policy="aac",
        title="Song",
        artist="Artist",
        album="Album",
    )
    aac = repository.ensure(descriptor)
    opus = repository.ensure(replace(descriptor, policy="opus"))
    assert aac.source == opus.source == descriptor.source
    assert (aac.policy, aac.container) == ("aac", "m4a")
    assert (opus.policy, opus.container) == ("opus", "webm")
    assert {item.policy for item in repository.list()} == {"aac", "opus"}


def test_media_repository_transitions_and_updates_physical_metadata(tmp_path):
    repository, _ = make_repository(tmp_path)
    repository.ensure(
        MediaDescriptor(
            source="khnokW3Mw24",
            policy="aac",
            title="Song",
            artist="Artist",
        )
    )
    writing = repository.transition(
        "khnokW3Mw24",
        "aac",
        "writing",
        relative_path="Artist/Album/01 - Song.m4a",
    )
    time.sleep(0.001)
    ready = repository.transition(
        writing.source,
        writing.policy,
        "ready",
        relative_path=writing.relative_path,
        size=1234,
        checksum="sha256:abc",
        provider_identity="youtube:khnokW3Mw24",
    )
    assert ready.state == "ready"
    assert ready.size == 1234
    assert ready.relative_path == "Artist/Album/01 - Song.m4a"
    assert ready.updated >= writing.updated
    assert repository.delete(ready.source, ready.policy)
    assert repository.get(ready.source, ready.policy) is None


def test_media_repository_rejects_invalid_records_and_transitions(tmp_path):
    repository, _ = make_repository(tmp_path)
    invalid = MediaFile(
        source="khnokW3Mw24",
        policy="mp3",
        relative_path=None,
        container="mp3",
        size=None,
        checksum=None,
        provider_identity=None,
        state="pending",
        created=0,
        updated=0,
    )
    with pytest.raises(ValueError, match="Unknown audio policy"):
        repository.save(invalid)
    with pytest.raises(ValueError, match="Unknown media file state"):
        repository.transition("khnokW3Mw24", "aac", "unknown")


def test_saved_paths_sanitize_unicode_missing_metadata_and_long_names():
    descriptor = MediaDescriptor(
        source="khnokW3Mw24",
        policy="aac",
        title="Song / *?",
        artist="Beyoncé / Live",
        album="Album: Deluxe",
        track_number=3,
    )
    path = saved_relative_path(descriptor)
    artist, album, filename = path.split("/")
    assert artist == "Beyoncé - Live"
    assert album == "Album- Deluxe"
    assert filename == "03 - Song - -- [aac].m4a"
    assert all(len(part) <= 96 for part in path.split("/"))
    assert ".." not in path

    long = saved_relative_path(
        replace(
            descriptor,
            artist="A" * 200,
            album="B" * 200,
            title="C" * 200,
        )
    )
    assert all(len(part) <= 96 for part in long.split("/"))
    assert all("~" in part for part in long.split("/")[:2])

    missing = saved_relative_path(
        replace(descriptor, artist="", album=None, title="")
    )
    assert missing.startswith("Unknown Artist/Unknown Album/")
    assert "khnokW3Mw24" in missing


def test_saved_paths_disambiguate_collisions_and_policies():
    descriptor = MediaDescriptor(
        source="khnokW3Mw24",
        policy="aac",
        title="Song",
        artist="Artist",
        album="Album",
    )
    first = saved_relative_path(descriptor)
    collision = saved_relative_path(
        replace(descriptor, source="HYXGY67fATk"), [first]
    )
    opus = saved_relative_path(
        replace(descriptor, source="HYXGY67fATk", policy="opus"),
        [first, collision],
    )
    assert first.endswith("Song [aac].m4a")
    assert collision.endswith("(HYXGY67fATk).m4a")
    assert opus.endswith("Song [opus].webm")


def test_media_placement_persists_path_once_and_separates_policies(tmp_path):
    repository, _ = make_repository(tmp_path)
    placement = MediaPlacement(repository)
    descriptor = MediaDescriptor(
        source="khnokW3Mw24",
        policy="aac",
        title="Original",
        artist="Artist",
        album="Album",
    )
    first = placement.ensure(descriptor)
    changed_metadata = placement.ensure(
        replace(descriptor, title="Changed after save")
    )
    opus = placement.ensure(replace(descriptor, policy="opus"))
    assert first.relative_path == changed_metadata.relative_path
    assert first.relative_path != opus.relative_path
    assert first.relative_path.endswith("[aac].m4a")
    assert opus.relative_path.endswith("[opus].webm")


@pytest.mark.parametrize(
    ("policy", "extension", "tag", "container"),
    [
        ("aac", ".m4a", "title", "mp4"),
        ("opus", ".webm", "TITLE", "webm"),
    ],
)
def test_metadata_writer_uses_format_tags_without_transcoding(
    tmp_path, monkeypatch, policy, extension, tag, container
):
    calls = []

    def run(command, **options):
        calls.append((command, options))
        Path(command[-1]).write_bytes(b"tagged audio")

    monkeypatch.setattr(
        "lastbridge.features.playback.adapters.filesystem.metadata"
        ".subprocess.run",
        run,
    )
    source = tmp_path / f"source{extension}"
    destination = tmp_path / "Artist" / f"saved{extension}"
    source.write_bytes(b"source audio")
    descriptor = MediaDescriptor(
        source="khnokW3Mw24",
        policy=policy,
        title="A title",
        artist="An artist",
        album="An album",
        album_artist="An album artist",
        track_number=4,
        disc_number=2,
        year=2026,
        genre="Electronic",
    )

    result = MetadataWriter().write(source, destination, descriptor)

    command, options = calls[0]
    assert result == destination
    assert destination.read_bytes() == b"tagged audio"
    assert command[command.index("-c") + 1] == "copy"
    assert command[-3:-1] == ["-f", container]
    assert f"{tag}=A title" in command
    assert options["check"] is True
    assert options["timeout"] == 60


def test_managed_storage_publishes_retained_audio_directly_to_saved_root(
    tmp_path,
):
    async def scenario():
        key = f"{SOURCE}-aac"
        audio, repository = make_managed_audio(tmp_path, {key})
        key, entry = await audio.acquire(SOURCE)
        saved_path = audio.path(key, entry)
        record = repository.get(SOURCE, "aac")
        assert entry["saved"]
        assert saved_path.relative_to(tmp_path / "saved" / "audio").parts == (
            "Artist",
            "Album",
            "01 - Song [aac].m4a",
        )
        assert record.state == "ready"
        assert (
            record.relative_path
            == saved_path.relative_to(tmp_path / "saved" / "audio").as_posix()
        )
        assert not list((tmp_path / "cache" / "audio").glob("*.m4a"))
        await audio.release(key)
        await audio.close()

    asyncio.run(scenario())


def test_managed_promotion_preserves_active_lease_and_unpin_deletes_saved(
    tmp_path,
):
    async def scenario():
        key = f"{SOURCE}-aac"
        audio, repository = make_managed_audio(tmp_path)
        key, entry = await audio.acquire(SOURCE)
        cache_path = audio.path(key, entry)
        audio.storage.pins.add(key)
        assert await audio.retain(key)
        saved_path = audio.path(key, audio.entries[key])
        assert saved_path.exists() and cache_path.exists()
        await audio.release(key)
        assert saved_path.exists() and not cache_path.exists()
        await audio.update_pins(set())
        assert not saved_path.exists()
        assert repository.get(SOURCE, "aac") is None
        assert key not in audio.entries
        await audio.close()

    asyncio.run(scenario())


def test_managed_restart_marks_missing_saved_file_and_reacquires_same_path(
    tmp_path,
):
    async def scenario():
        key = f"{SOURCE}-aac"
        audio, repository = make_managed_audio(tmp_path, {key})
        key, entry = await audio.acquire(SOURCE)
        saved_path = audio.path(key, entry)
        await audio.release(key)
        await audio.close()
        saved_path.unlink()

        replacement, repository = make_managed_audio(tmp_path, {key})
        assert not replacement.entries
        assert repository.get(SOURCE, "aac").state == "missing"
        key, entry = await replacement.acquire(SOURCE)
        assert entry["saved"]
        assert replacement.path(key, entry) == saved_path
        assert repository.get(SOURCE, "aac").state == "ready"
        await replacement.release(key)
        await replacement.close()

    asyncio.run(scenario())


def test_managed_publication_failure_leaves_retryable_media_record(tmp_path):
    async def scenario():
        key = f"{SOURCE}-aac"

        class FullDisk:
            def write(self, source, destination, descriptor):
                raise OSError("disk full")

        audio, repository = make_managed_audio(
            tmp_path, {key}, metadata_writer=FullDisk()
        )
        with pytest.raises(CatalogError, match="acquisition failed"):
            await audio.acquire(SOURCE)
        record = repository.get(SOURCE, "aac")
        assert record.state == "pending"
        assert not audio.entries
        assert not list((tmp_path / "saved" / "audio").rglob("*.m4a"))
        await audio.close()

    asyncio.run(scenario())


def test_ready_notifier_runs_after_saved_media_is_ready(tmp_path):
    async def scenario():
        calls = []

        class Notifier:
            async def notify(self):
                calls.append(True)

            async def close(self):
                pass

        key = f"{SOURCE}-aac"
        audio, repository = make_managed_audio(
            tmp_path, {key}, ready_notifier=Notifier()
        )
        key, entry = await audio.acquire(SOURCE)
        assert entry["saved"]
        assert repository.get(SOURCE, "aac").state == "ready"
        assert calls == [True]
        await audio.release(key)
        await audio.close()

    asyncio.run(scenario())


def test_jellyfin_notifier_posts_refresh_request():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(204, request=request)

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        notifier = JellyfinScanNotifier(
            "http://jellyfin:8096/Library/Refresh",
            "test-api-key",
            client=client,
        )
        await notifier.notify()
        await notifier.close()

    asyncio.run(scenario())
    request = requests[0]
    assert request.url.path == "/Library/Refresh"
    assert request.headers["X-Emby-Token"] == "test-api-key"


def test_cached_track_promotion_notifies_after_saved_commit(tmp_path):
    async def scenario():
        calls = []

        class Notifier:
            async def notify(self):
                assert not audio.storage.lock.locked()
                assert repository.get(SOURCE, "aac").state == "ready"
                calls.append(True)

            async def close(self):
                pass

        audio, repository = make_managed_audio(
            tmp_path, ready_notifier=Notifier()
        )
        key, entry = await audio.acquire(SOURCE)
        assert not entry["saved"] and not calls
        audio.storage.pins.add(key)
        assert await audio.retain(key)
        assert calls == [True]
        await audio.release(key)
        await audio.close()

    asyncio.run(scenario())
