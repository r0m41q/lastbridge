import asyncio
import os
import threading
from dataclasses import replace

import pytest
from support.artwork import PNG
from support.audio import SOURCE
from support.build_accounts import make_accounts
from support.enrichment import audio_packets, make_enrichment

from lastbridge.features.playback.adapters.filesystem.metadata import (
    MetadataWriter,
)
from lastbridge.features.playback.application.artwork import SavedArtwork
from lastbridge.features.playback.domain.enrichment import lyric_file
from lastbridge.shared.errors import CatalogError, StorageError


@pytest.mark.parametrize("policy", ["aac", "opus"])
def test_repairs_tags_lyrics_and_optional_art_without_changing_audio(
    tmp_path, policy
):
    async def scenario():
        svc, audio, record, path = make_enrichment(tmp_path, policy)
        before = audio_packets(path)
        assert svc.report(SOURCE, policy)["tags"] == "unchecked"
        result = await svc.process(
            SOURCE, policy, repair=True, embed_cover=True, embed_lyrics=True
        )
        assert (
            result["tags"] == result["cover"] == result["lyrics"] == "present"
        )
        assert result["embedded_cover"] is (policy == "aac")
        assert result["embedded_lyrics"]
        assert path.with_suffix(".lrc").read_text() == (
            "[00:01.234]First lyric\n[01:01.234]Second lyric\n"
        )
        assert audio_packets(path) == before
        updated = svc.media.get(SOURCE, policy)
        assert updated.size == path.stat().st_size
        assert updated.relative_path == record.relative_path
        assert audio.entries[f"{SOURCE}-{policy}"]["size"] == updated.size
        assert svc.report(SOURCE, policy)["tags"] == "present"
        assert svc.files.writer.inspect(
            path, audio.storage.descriptor_factory(SOURCE, policy)
        )["embedded_lyrics"]
        # Repeated tag repair preserves embedded content and manual sidecars.
        path.with_suffix(".lrc").write_text("User edited lyrics")
        repeated = await svc.process(SOURCE, policy, repair=True)
        assert repeated["embedded_cover"] is (policy == "aac")
        assert repeated["embedded_lyrics"]
        assert audio_packets(path) == before
        assert path.with_suffix(".lrc").read_text() == "User edited lyrics"
        assert not list(path.parent.glob(".lastbridge-*"))
        assert not audio.provider.calls
        await audio.close()

    asyncio.run(scenario())


def test_automatic_enrichment_does_not_rewrite_audio_and_survives_restart(
    tmp_path,
):
    async def scenario():
        svc, audio, record, path = make_enrichment(tmp_path)
        before = path.read_bytes()
        worker = SavedArtwork(
            svc.media, svc.artwork, svc.covers, enrichment=svc
        )
        assert await worker.populate()
        assert path.read_bytes() == before
        assert svc.report(SOURCE, "aac")["tags"] == "needs repair"
        assert svc.report(SOURCE, "aac")["lyrics"] == "present"
        assert not await worker.populate()
        assert svc.lyrics.calls == ["selected-recording"]
        accounts = make_accounts(tmp_path / "data")
        with accounts.repository.database.connection() as connection:
            assert (
                connection.execute("PRAGMA user_version").fetchone()[0] == 24
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM media_enrichment"
                ).fetchone()[0]
                == 1
            )
        await audio.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("suffix", [".lrc", ".elrc", ".txt"])
def test_preserves_user_lyrics_and_cover(tmp_path, suffix):
    async def scenario():
        svc, audio, record, path = make_enrichment(tmp_path)
        path.with_suffix(suffix).write_text("Manual lyrics")
        cover = path.parent / "Folder.PNG"
        cover.write_bytes(PNG)
        result = await svc.process(SOURCE, "aac", repair=True)
        assert result["cover"] == result["lyrics"] == "present"
        assert path.with_suffix(suffix).read_text() == "Manual lyrics"
        assert cover.read_bytes() == PNG
        assert not svc.lyrics.calls and not svc.artwork.calls
        assert not list(path.parent.glob("cover.*"))
        await audio.close()

    asyncio.run(scenario())


def test_plain_missing_and_failed_lyrics_do_not_fail_repair(tmp_path):
    async def scenario():
        svc, audio, record, path = make_enrichment(tmp_path)
        svc.lyrics.documents = [{"line": [{"value": "Plain lyrics"}]}]
        await svc.process(SOURCE, "aac", repair=True, embed_lyrics=True)
        assert path.with_suffix(".txt").read_text() == "Plain lyrics\n"
        path.with_suffix(".txt").unlink()
        svc.lyrics.documents = []
        result = await svc.process(SOURCE, "aac", repair=True)
        assert result["lyrics"] == "not found"
        assert svc.report(SOURCE, "aac")["lyrics"] == "not found"
        svc.artwork.fail_sources.add(SOURCE)
        (path.parent / "cover.png").unlink()

        async def fail(song_id):
            raise CatalogError(0, "Private upstream URL")

        svc.lyrics.get = fail
        result = await svc.process(SOURCE, "aac", repair=True)
        assert result["lyrics"] == "unavailable"
        assert result["tags"] == "present"
        assert result["cover"] == "missing"
        await audio.close()

    asyncio.run(scenario())


def test_active_playback_defers_repair_and_keeps_bytes(tmp_path):
    async def scenario():
        svc, audio, record, path = make_enrichment(tmp_path)
        before = path.read_bytes()
        key, _ = await audio.acquire(SOURCE)
        with pytest.raises(CatalogError, match="playing"):
            await svc.process(SOURCE, "aac", repair=True)
        assert path.read_bytes() == before
        assert audio.storage.leases[key] == 1
        await audio.release(key)
        await svc.process(SOURCE, "aac", repair=True)
        await audio.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["writer", "database", "inspect"])
def test_repair_failure_preserves_audio_and_inventory(tmp_path, failure):
    async def scenario():
        svc, audio, record, path = make_enrichment(tmp_path)
        before = path.read_bytes()

        def fail(*args, **kwargs):
            raise StorageError("private database failure")

        if failure == "database":
            audio.storage._transition_media = fail
        elif failure == "writer":
            svc.files.writer.write = fail
        else:
            svc.files.writer.inspect = fail
        with pytest.raises(StorageError):
            await svc.process(SOURCE, "aac", repair=True)
        assert path.read_bytes() == before
        assert svc.media.get(SOURCE, "aac") == record
        assert audio.entries[f"{SOURCE}-aac"]["size"] == record.size
        assert not audio.storage.leases
        assert not list(path.parent.glob(".lastbridge-*"))
        await audio.close()

    asyncio.run(scenario())


def test_cancellation_joins_writer_before_releasing_file(tmp_path):
    async def scenario():
        svc, audio, record, path = make_enrichment(tmp_path)
        before = path.read_bytes()
        started, resume = threading.Event(), threading.Event()
        writer = svc.files.writer.write

        def delayed(*args, **kwargs):
            started.set()
            assert resume.wait(10)
            return writer(*args, **kwargs)

        svc.files.writer.write = delayed
        task = asyncio.create_task(svc.process(SOURCE, "aac", repair=True))
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and audio.storage.leases
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert path.read_bytes() == before
        assert not audio.storage.leases
        assert not list(path.parent.glob(".lastbridge-*"))
        await audio.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["unpin", "lease", "edit"])
def test_concurrent_changes_cannot_publish_stale_repairs(tmp_path, change):
    async def scenario():
        svc, audio, record, path = make_enrichment(tmp_path)
        before = path.read_bytes()
        started, resume = threading.Event(), threading.Event()
        writer = svc.files.writer.write

        def delayed(*args, **kwargs):
            started.set()
            assert resume.wait(10)
            return writer(*args, **kwargs)

        svc.files.writer.write = delayed
        task = asyncio.create_task(svc.process(SOURCE, "aac", repair=True))
        assert await asyncio.to_thread(started.wait, 10)
        key = f"{SOURCE}-aac"
        if change == "unpin":
            await audio.update_pins(set())
        elif change == "lease":
            await audio.acquire(SOURCE)
        else:
            path.write_bytes(before + b"user change")
        resume.set()
        with pytest.raises(CatalogError):
            await task
        if change == "unpin":
            assert not path.exists() and svc.media.get(SOURCE, "aac") is None
        else:
            assert path.read_bytes() == before + (
                b"user change" if change == "edit" else b""
            )
        if change == "lease":
            await audio.release(key)
        assert not audio.storage.leases
        assert not list(path.parent.glob(".lastbridge-*"))
        await audio.close()

    asyncio.run(scenario())


def test_unmanaged_and_symlink_files_are_never_repaired(tmp_path):
    async def scenario():
        svc, audio, record, path = make_enrichment(tmp_path)
        unrelated = path.parent / "unmanaged.m4a"
        unrelated.write_bytes(b"Unrelated audio")
        outside = tmp_path / "outside.m4a"
        path.replace(outside)
        path.symlink_to(outside)
        assert svc.report(SOURCE, "aac")["audio"] == "missing"
        with pytest.raises((OSError, ValueError)):
            await svc.process(SOURCE, "aac", repair=True)
        assert unrelated.read_bytes() == b"Unrelated audio"
        assert not svc.lyrics.calls
        await audio.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_publication_recovers_using_committed_inventory(
    tmp_path, committed
):
    async def scenario():
        svc, audio, record, path = make_enrichment(tmp_path)
        before = path.read_bytes()
        await audio.close()
        backup = path.with_name(f".lastbridge-repair-{path.name}.bak")
        os.link(path, backup)
        path.unlink()
        repaired = before + b"repaired tags"
        path.write_bytes(repaired)
        if committed:
            svc.media.save(replace(record, size=len(repaired)))
        # Use the real startup restore path, without generating new audio.
        from lastbridge.features.playback.adapters.filesystem.storage import (
            AudioFiles,
        )

        replacement = AudioFiles(
            tmp_path / "managed-cache",
            reserve=0,
            saved_directory=path.parents[2],
            media_files=svc.media,
            media_placement=audio.storage.media_placement,
            metadata_writer=MetadataWriter(),
            descriptor_factory=audio.storage.descriptor_factory,
        )
        assert path.read_bytes() == (repaired if committed else before)
        assert not backup.exists()
        assert replacement.entries[f"{SOURCE}-aac"]["saved"]
        replacement.close()

    asyncio.run(scenario())


def test_enrichment_migration_and_media_removal_cascade(tmp_path):
    async def scenario():
        svc, audio, record, path = make_enrichment(tmp_path)
        db = svc.repository.database
        with db.transaction() as connection:
            connection.execute("DROP TABLE media_enrichment")
            connection.execute("PRAGMA user_version=20")
        make_accounts(tmp_path / "data")
        assert svc.media.get(SOURCE, "aac") == record
        await svc.process(SOURCE, "aac")
        assert svc.repository.get(SOURCE, "aac")
        await audio.update_pins(set())
        assert not svc.repository.get(SOURCE, "aac")
        await audio.close()

    asyncio.run(scenario())


def test_lyric_serialization_keeps_millisecond_timing():
    assert lyric_file([]) == (None, "")
    assert lyric_file([{"line": []}]) == (None, "")
    assert lyric_file(
        [
            {
                "synced": True,
                "line": [
                    {"start": 0, "value": "Intro"},
                    {"start": 123456, "value": "♪"},
                ],
            }
        ]
    ) == (".lrc", "[00:00.000]Intro\n[02:03.456]♪\n")


def test_catalog_changes_invalidate_tag_status(tmp_path):
    async def scenario():
        svc, audio, record, path = make_enrichment(tmp_path)
        await svc.process(SOURCE, "aac", repair=True)
        assert svc.report(SOURCE, "aac")["tags"] == "present"
        factory = svc.descriptor
        svc.descriptor = lambda source, policy: replace(
            factory(source, policy), title="Corrected title"
        )
        assert svc.report(SOURCE, "aac")["tags"] == "unchecked"
        await svc.process(SOURCE, "aac", repair=True)
        assert svc.report(SOURCE, "aac")["tags"] == "present"
        await audio.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("edited", [False, True])
def test_release_removes_only_unchanged_generated_lyrics(tmp_path, edited):
    async def scenario():
        svc, audio, record, path = make_enrichment(tmp_path)
        await svc.process(SOURCE, "aac", repair=True)
        sidecar = path.with_suffix(".lrc")
        manual = path.with_suffix(".txt")
        manual.write_text("Unrelated manual lyrics")
        if edited:
            sidecar.write_text("User changed these lyrics")
        await audio.update_pins(set())
        assert sidecar.exists() is edited
        assert manual.read_text() == "Unrelated manual lyrics"
        assert not path.exists()
        assert svc.repository.get(SOURCE, "aac") == {}
        await audio.close()

    asyncio.run(scenario())


def test_status_write_failure_does_not_leave_unowned_lyrics(tmp_path):
    async def scenario():
        svc, audio, record, path = make_enrichment(tmp_path)

        def fail(*args):
            raise StorageError("Private database failure")

        svc.repository.save = fail
        with pytest.raises(StorageError):
            await svc.process(SOURCE, "aac", repair=True)
        assert not path.with_suffix(".lrc").exists()
        assert svc.media.get(SOURCE, "aac").size == path.stat().st_size
        assert not audio.storage.leases
        assert not list(path.parent.glob(".lastbridge-*"))
        await audio.close()

    asyncio.run(scenario())
