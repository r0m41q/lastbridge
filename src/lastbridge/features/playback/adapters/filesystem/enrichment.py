import asyncio
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import anyio

from lastbridge.features.playback.adapters.filesystem.artwork import (
    COVER_NAMES,
)
from lastbridge.shared.errors import CatalogError


class EnrichmentFiles:
    """Inspect off-loop; publish only while a managed file has no readers."""

    def __init__(self, audio, covers, writer):
        self.audio, self.covers, self.writer = audio, covers, writer
        self.storage = audio.storage

    def path(self, record):
        raw = self.covers.root / record.relative_path
        path = self.covers.audio(record.relative_path)
        if path != raw.absolute() or not path.is_file():
            raise OSError("Saved audio missing")
        return path

    def signature(self, record):
        stat = self.path(record).stat()
        return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]

    def cover(self, record):
        for image in self.path(record).parent.iterdir():
            if image.name.casefold() in COVER_NAMES:
                resolved = image.resolve()
                if (
                    resolved.is_relative_to(self.covers.root)
                    and resolved.is_file()
                    and resolved.stat().st_size <= 2 * 1024 * 1024
                ):
                    return resolved
        return None

    def lyrics(self, record):
        path = self.path(record)
        for suffix in (".lrc", ".elrc", ".txt"):
            candidate = path.with_suffix(suffix)
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        return None

    def status(self, record):
        return {
            "signature": self.signature(record),
            "cover": "present" if self.cover(record) else "missing",
            "lyrics": "present" if self.lyrics(record) else "missing",
        }

    def current(self, record):
        current = self.storage.media_files.get(record.source, record.policy)
        key = f"{record.source}-{record.policy}"
        return (
            current == record
            and record.state == "ready"
            and key in self.storage.pins
            and self.storage.entries.get(key, {}).get("saved")
        )

    async def apply(
        self,
        record,
        descriptor,
        suffix,
        text,
        *,
        repair=False,
        embed_cover=False,
        embed_lyrics=False,
        publish_status=None,
    ):
        key = f"{record.source}-{record.policy}"
        async with self.storage.lock:
            if not self.current(record):
                raise CatalogError(70, "Saved recording no longer available")
            if self.storage.leases[key]:
                raise CatalogError(0, "Recording is playing; retry later")
            path = self.path(record)
            signature = self.signature(record)
            store = self.storage.enrichment_store
            owned = (
                store.get(record.source, record.policy).get("owned_lyrics")
                if store is not None
                else None
            )
            if repair and shutil.disk_usage(path.parent).free < (
                self.storage.reserve + record.size + 3 * 1024 * 1024
            ):
                raise OSError("Not enough free space for repair")
            self.storage.leases[key] += 1
        directory = None
        # A canceled request must not leave a writer running after its lease
        # or staging directory has been released.
        with anyio.CancelScope(shield=True):
            try:
                directory = Path(
                    tempfile.mkdtemp(
                        prefix=".lastbridge-enrich-", dir=path.parent
                    )
                )
                cover = self.cover(record) if embed_cover else None
                supported = (
                    record.policy == "aac"
                    and cover is not None
                    and cover.suffix.casefold() in {".jpg", ".jpeg", ".png"}
                )
                if not supported:
                    cover = None
                existing = self.lyrics(record)
                embedded = text
                if embed_lyrics and existing:
                    if existing.stat().st_size > 256 * 1024:
                        raise ValueError("Lyrics file too large")
                    embedded = existing.read_text(encoding="utf-8")
                output = directory / path.name

                def prepare():
                    if repair:
                        self.writer.write(
                            path,
                            output,
                            descriptor,
                            cover=cover,
                            lyrics=embedded
                            if embed_lyrics and embedded
                            else None,
                        )
                    return self.writer.inspect(
                        output if repair else path, descriptor
                    )

                task = asyncio.create_task(anyio.to_thread.run_sync(prepare))
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    # asyncio task cancellation also needs to join the writer.
                    await task
                    raise
                async with self.storage.lock:
                    if not self.current(record) or signature != self.signature(
                        record
                    ):
                        raise CatalogError(
                            70, "Saved recording changed; retry"
                        )
                    if self.storage.leases[key] != 1:
                        raise CatalogError(
                            0, "Recording is playing; retry later"
                        )
                    if repair:
                        self.commit(record, path, output)
                    created = None
                    if suffix and text and not self.lyrics(record):
                        sidecar = directory / (path.stem + suffix)
                        sidecar.write_text(text, encoding="utf-8")
                        try:
                            os.link(sidecar, path.with_suffix(suffix))
                        except FileExistsError:
                            pass
                        else:
                            created = path.with_suffix(suffix)
                            owned = {
                                "name": path.with_suffix(suffix).name,
                                "sha256": hashlib.sha256(
                                    text.encode("utf-8")
                                ).hexdigest(),
                            }
                    result.update(self.status(record))
                    if owned:
                        result["owned_lyrics"] = owned
                    result["artwork_embedding"] = (
                        "supported" if supported else "sidecar only"
                    )
                    if publish_status is not None:
                        try:
                            publish_status(result)
                        except BaseException:
                            # An unrecorded sidecar must not be mistaken for
                            # manually supplied lyrics on a later path reuse.
                            if (
                                created is not None
                                and not created.is_symlink()
                                and created.read_bytes()
                                == text.encode("utf-8")
                            ):
                                created.unlink()
                            raise
                    return result
            finally:
                if directory:
                    shutil.rmtree(directory, ignore_errors=True)
                await self.audio.release(key)

    def commit(self, record, path, output):
        size = output.stat().st_size
        if size <= 0:
            raise OSError("Empty repaired audio")
        if shutil.disk_usage(path.parent).free < self.storage.reserve:
            raise OSError("Not enough free space for repair")
        backup = path.with_name(f".lastbridge-repair-{path.name}.bak")
        os.link(path, backup)
        try:
            os.replace(output, path)
            self.storage._transition_media(
                record, "ready", size=size, checksum=None
            )
        except BaseException:
            os.replace(backup, path)
            raise
        else:
            key = f"{record.source}-{record.policy}"
            self.storage.entries[key]["size"] = size
            backup.unlink(missing_ok=True)
