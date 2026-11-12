import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import asdict

from lastbridge.features.playback.application.ports import (
    EnrichmentStorage,
    EnrichmentStore,
    MediaFiles,
    SavedCatalog,
    SavedLyrics,
)
from lastbridge.features.playback.domain.enrichment import lyric_file
from lastbridge.shared.errors import CatalogError, StorageError

logger = logging.getLogger(__name__)


class SavedEnrichment:
    def __init__(
        self,
        repository: EnrichmentStore,
        media: MediaFiles,
        files: EnrichmentStorage,
        catalog: SavedCatalog,
        lyrics: SavedLyrics,
        descriptor: Callable,
        artwork=None,
        covers=None,
    ):
        self.repository, self.media, self.files = repository, media, files
        self.catalog, self.lyrics, self.descriptor = (
            catalog,
            lyrics,
            descriptor,
        )
        self.artwork, self.covers = artwork, covers
        self.lock = asyncio.Lock()

    def report(self, source, policy):
        record = self.media.get(source, policy)
        result = {
            "audio": "missing",
            "tags": "unchecked",
            "cover": "missing",
            "lyrics": "missing",
            "embedded_cover": False,
            "embedded_lyrics": False,
        }
        if not record or record.state != "ready":
            return result
        try:
            actual = self.files.status(record)
            stored = self.repository.get(source, policy)
            if stored.get("signature") == actual["signature"]:
                result.update(stored)
                if stored.get("descriptor") != asdict(
                    self.descriptor(source, policy)
                ):
                    result["tags"] = "unchecked"
            result.update(actual)
            if (
                stored.get("signature") == actual["signature"]
                and actual["lyrics"] == "missing"
                and stored.get("lyrics")
                in (
                    "unavailable",
                    "not found",
                )
            ):
                result["lyrics"] = stored["lyrics"]
            result["audio"] = "present"
        except OSError, ValueError:
            pass
        return result

    async def populate(self, record):
        """Fill lyric sidecars and inspect tags without rewriting audio."""
        report = self.report(record.source, record.policy)
        if (
            report.get("checked", 0) > time.time() - 86400
            and report["tags"] != "unchecked"
        ):
            return False
        if self.lock.locked():
            return False
        try:
            await self.process(record.source, record.policy)
            return report["lyrics"] == "missing" and bool(
                self.files.lyrics(record)
            )
        except CatalogError, StorageError, OSError, ValueError, RuntimeError:
            logger.warning("Saved file enrichment unavailable; will retry")
            return False

    async def process(
        self,
        source,
        policy,
        *,
        repair=False,
        embed_cover=False,
        embed_lyrics=False,
    ):
        if self.lock.locked():
            raise CatalogError(0, "Saved file repair busy; retry later")
        async with self.lock:
            record = self.media.get(source, policy)
            if record is None or record.state != "ready":
                raise CatalogError(70, "Saved recording not found")
            suffix, text = None, ""
            unavailable = False
            if not self.files.lyrics(record):
                try:
                    song = self.catalog.read_source(source, "song")
                    suffix, text = lyric_file(
                        await self.lyrics.get(song["id"])
                    )
                except CatalogError, StorageError, OSError, ValueError:
                    unavailable = True
            if (
                repair
                and self.covers is not None
                and self.covers.needed(record.relative_path)
            ):
                try:
                    data, media_type = await self.artwork.fetch(source)
                    if self.media.get(source, policy) == record:
                        self.covers.write(
                            record.relative_path, data, media_type
                        )
                except CatalogError, StorageError, OSError, ValueError:
                    pass
            descriptor = self.descriptor(source, policy)

            def publish(result):
                result["descriptor"] = asdict(descriptor)
                if result["lyrics"] == "missing":
                    result["lyrics"] = (
                        "unavailable" if unavailable else "not found"
                    )
                self.repository.save(source, policy, result)

            result = await self.files.apply(
                record,
                descriptor,
                suffix,
                text,
                repair=repair,
                embed_cover=embed_cover,
                embed_lyrics=embed_lyrics,
                publish_status=publish,
            )
            return result
