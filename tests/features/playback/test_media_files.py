import time
from dataclasses import replace

import pytest

from lastbridge.features.playback.adapters.sqlite.repository import (
    MediaFileRepository,
)
from lastbridge.features.playback.domain.models import (
    MediaDescriptor,
    MediaFile,
)
from lastbridge.platform.credentials import Credentials
from lastbridge.platform.database import Database
from lastbridge.platform.migrations import migrate


def make_repository(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    credentials = Credentials(directory, directory / "lastbridge.sqlite3")
    database = Database(directory)
    migrate(database, credentials)
    return MediaFileRepository(database), database


def test_media_migration_creates_version_11_table(tmp_path):
    repository, database = make_repository(tmp_path)
    with database.connection() as connection:
        assert connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0] == 11
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
