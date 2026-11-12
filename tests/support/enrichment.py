import subprocess
from dataclasses import replace

import pytest

from lastbridge.features.playback.adapters.filesystem.artwork import (
    AlbumCovers,
)
from lastbridge.features.playback.adapters.filesystem.enrichment import (
    EnrichmentFiles,
)
from lastbridge.features.playback.adapters.filesystem.metadata import (
    MetadataWriter,
)
from lastbridge.features.playback.adapters.filesystem.storage import AudioFiles
from lastbridge.features.playback.adapters.http.response import AudioResponse
from lastbridge.features.playback.adapters.sqlite.enrichment import (
    EnrichmentRepository,
)
from lastbridge.features.playback.adapters.sqlite.repository import (
    MediaFileRepository,
)
from lastbridge.features.playback.application.enrichment import SavedEnrichment
from lastbridge.features.playback.application.media import MediaPlacement
from lastbridge.features.playback.application.service import AudioCache
from lastbridge.features.playback.domain.models import MediaDescriptor
from support.artwork import FakeArtwork
from support.audio import SOURCE, FakeAudio
from support.build_accounts import make_accounts


class SavedLyricFixture:
    def __init__(self):
        self.calls = []
        self.documents = [
            {
                "synced": True,
                "offset": 0,
                "line": [
                    {"start": 1234, "value": "First lyric"},
                    {"start": 61234, "value": "Second lyric"},
                ],
            }
        ]

    async def get(self, song_id):
        self.calls.append(song_id)
        return self.documents


def audio_packets(path):
    return subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_packets",
            "-show_data_hash",
            "sha256",
            "-show_entries",
            "packet=data_hash",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout


def make_enrichment(tmp_path, policy="aac", database=None, catalog=None):
    if database is None:
        database = make_accounts(tmp_path / "data").repository.database
    media = MediaFileRepository(database)
    descriptor = MediaDescriptor(
        SOURCE,
        policy,
        "Song",
        "Artist",
        album="Album",
        album_artist="Album Artist",
        track_number=2,
        disc_number=1,
        year=2026,
    )
    root = tmp_path / "saved" / "audio"
    record = MediaPlacement(media).ensure(descriptor)
    path = root / record.relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.2",
            "-c:a",
            "aac" if policy == "aac" else "libopus",
            str(path),
        ],
        check=True,
        timeout=10,
    )
    record = media.transition(
        SOURCE,
        policy,
        "ready",
        relative_path=record.relative_path,
        size=path.stat().st_size,
        provider_identity="test",
    )
    storage = AudioFiles(
        tmp_path / "managed-cache",
        reserve=0,
        policy=policy,
        saved_directory=root,
        pins={f"{SOURCE}-{policy}"},
        media_files=media,
        media_placement=MediaPlacement(media),
        metadata_writer=MetadataWriter(),
        descriptor_factory=lambda source, rendition: replace(
            descriptor, source=source, policy=rendition
        ),
        enrichment_store=EnrichmentRepository(database),
    )
    audio = AudioCache(storage, FakeAudio(), AudioResponse)
    covers = AlbumCovers(root)
    files = EnrichmentFiles(audio, covers, MetadataWriter())
    if catalog is None:

        class Catalog:
            def read_source(self, source, kind):
                return {"id": "selected-recording"}

        catalog = Catalog()
    lyrics, artwork = SavedLyricFixture(), FakeArtwork()
    service = SavedEnrichment(
        EnrichmentRepository(database),
        media,
        files,
        catalog,
        lyrics,
        storage.descriptor_factory,
        artwork,
        covers,
    )
    return service, audio, record, path


@pytest.fixture
def enrichment_service(saved_service, tmp_path):
    client, app, _, _ = saved_service
    svc = app.state.services
    service, audio, record, path = make_enrichment(
        tmp_path, database=svc.database, catalog=svc.catalog
    )
    svc.library.audio = audio
    app.state.services = replace(svc, enrichment=service, audio=audio)
    yield saved_service, service, audio, record, path
    client.portal.call(audio.close)
