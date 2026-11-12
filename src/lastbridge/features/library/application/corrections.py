import asyncio
import hashlib
import json

from lastbridge.features.library.application.ports import (
    CatalogAccess,
    CorrectionLibrary,
    CorrectionStore,
    RecordingSearch,
)
from lastbridge.features.library.domain.recordings import metadata
from lastbridge.shared.errors import CatalogError


class RecordingCorrections:
    def __init__(
        self,
        repository: CorrectionStore,
        catalog: CatalogAccess,
        library: CorrectionLibrary,
        provider: RecordingSearch,
    ):
        self.repository, self.catalog = repository, catalog
        self.library, self.provider = library, provider
        self.slots = asyncio.Semaphore(2)

    def context(self, user_id, kind, key):
        context = self.repository.correction_context(user_id, kind, key)
        song_id = context.get("song_id")
        original = self.catalog.read(song_id, "song") if song_id else None
        current = (
            self.library.recording(user_id, song_id, "song")
            if song_id
            else None
        )
        if original:
            document = original["document"]
            source = {
                "title": document["title"],
                "artists": [
                    {"name": a["name"]} for a in document.get("artists", [])
                ]
                or [{"name": document.get("artist") or "Unknown Artist"}],
                "duration_seconds": document.get("duration"),
                "isExplicit": document.get("isExplicit"),
            }
        else:
            source = {
                "title": context["title"],
                "artists": [{"name": context["artist"]}],
            }
        signature = {
            "source": source,
            "target": context.get("target"),
            "song_id": song_id,
            "recording_id": current["recording_id"] if current else None,
        }
        fingerprint = hashlib.sha256(
            json.dumps(signature, sort_keys=True).encode()
        ).hexdigest()
        return context | {
            "source": source,
            "original": original,
            "current": current,
            "fingerprint": fingerprint,
        }

    def report(self, user_id, kind, key):
        context = self.context(user_id, kind, key)
        review = self.repository.recording_review(user_id, kind, key)
        if review and review["fingerprint"] != context["fingerprint"]:
            review = None
        return context | {"review": review}

    async def search(self, user_id, kind, key, query=""):
        query = query.strip()
        if len(query) > 256:
            raise CatalogError(10, "Search is too long")
        context = self.context(user_id, kind, key)
        if self.slots.locked():
            raise CatalogError(0, "Recording search busy; retry shortly")
        try:
            async with self.slots, asyncio.timeout(20):
                result = await self.provider.resolve(
                    context["source"], query=query or None, review=True
                )
        except TimeoutError:
            raise CatalogError(
                0, "Recording search timed out; retry"
            ) from None
        if (
            self.context(user_id, kind, key)["fingerprint"]
            != context["fingerprint"]
        ):
            raise CatalogError(10, "Recording changed; search again")
        current = context["current"]
        candidates = [
            c
            for c in result["candidates"]
            if current is None or c["song_id"] != current["recording_id"]
        ]
        for candidate in candidates:
            candidate["metadata"] = metadata(
                self.catalog.read(candidate["song_id"], "song")["document"]
            )
        self.repository.save_review(
            user_id, kind, key, context["fingerprint"], candidates
        )

    async def choose(
        self, user_id, kind, key, token, recording_id, acknowledge=False
    ):
        context = self.context(user_id, kind, key)
        song_id = self.repository.choose_recording(
            user_id,
            kind,
            key,
            token,
            context["fingerprint"],
            recording_id,
            acknowledge,
        )
        # Durable membership, preference and job intent are already committed.
        self.library.notify()
        await self.library.reconcile()
        return song_id
