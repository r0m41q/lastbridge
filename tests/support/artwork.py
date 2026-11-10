import base64
import time

from lastbridge.features.playback.adapters.filesystem.artwork import (
    AlbumCovers,
)
from lastbridge.features.playback.adapters.sqlite.repository import (
    MediaFileRepository,
)
from lastbridge.features.playback.application.artwork import SavedArtwork
from lastbridge.features.playback.domain.models import MediaFile
from lastbridge.shared.errors import CatalogError
from support.audio import SOURCE
from support.build_accounts import make_accounts

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


class FakeArtwork:
    def __init__(self):
        self.calls = []
        self.fail_sources = set()

    async def fetch(self, source):
        self.calls.append(source)
        if source in self.fail_sources:
            raise CatalogError(0, "Private provider failure")
        return PNG, "image/png"


def make_saved_artwork(tmp_path, source=None, notifier=None):
    accounts = make_accounts(tmp_path / "data")
    repository = MediaFileRepository(accounts.repository.database)
    covers = AlbumCovers(tmp_path / "saved")
    source = source or FakeArtwork()
    return (
        SavedArtwork(repository, source, covers, notifier),
        repository,
        covers,
    )


def save_record(
    repository,
    root,
    source=SOURCE,
    relative_path="Artist/Album/01 - Track [aac].m4a",
    policy="aac",
    state="ready",
):
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"saved audio fixture")
    now = time.time()
    record = MediaFile(
        source=source,
        policy=policy,
        relative_path=relative_path,
        container="m4a" if policy == "aac" else "webm",
        size=path.stat().st_size,
        checksum=None,
        provider_identity=source,
        state=state,
        created=now,
        updated=now,
    )
    repository.save(record)
    return record
