import time

from lastbridge.features.lastfm_sync.application.ports import (
    CatalogAccess,
    LibraryAccess,
    SyncProvider,
    SyncStore,
)
from lastbridge.shared.errors import CatalogError, ProviderError
from lastbridge.shared.lastfm import LastfmRequestError


class LovedImports:
    def __init__(
        self,
        repository: SyncStore,
        catalog: CatalogAccess,
        library: LibraryAccess,
        remote: SyncProvider,
    ):
        self.repository, self.catalog, self.library, self.remote = (
            repository,
            catalog,
            library,
            remote,
        )

    async def import_one(self):
        now = time.time()
        claimed = self.repository.claim_import()
        if claimed is None:
            return False
        job, item = claimed
        try:
            if item:
                await self.resolve(job, item)
            elif (
                job["total_pages"] is None
                or job["next_page"] <= job["total_pages"]
            ):
                rows, total = await self.remote.loved(
                    job["target"], job["next_page"]
                )
                self.repository.save_page(job, rows, total)
            else:
                self.repository.finish_import(job)
        except LastfmRequestError as error:
            self.repository.fail_import(job, now, error)
        return True

    async def resolve(self, job, item):
        user_id, target, key = (
            item["user_id"],
            item["target"],
            item["recording_key"],
        )
        state, error, song_id = "pending", None, None
        try:
            removed = self.repository.removed(user_id, key)
            if removed:
                state, error = "skipped", "Removed locally."
            else:
                song = await self.catalog.match_loved(
                    item["artist"], item["title"]
                )
                if not self.repository.is_active(job):
                    return
                saved = await self.library.change(
                    user_id,
                    [(song["id"], "song")],
                    True,
                    imported=(target, key),
                )
                state = "matched" if saved else "skipped"
                song_id = song["id"]
                error = None if saved else "Removed locally."
        except ProviderError as failure:
            state = "unmatched"
            error = (
                "Multiple recordings match."
                if str(failure) == ("ambiguous_match")
                else "No confident recording match."
            )
        except CatalogError, OSError:
            state = "unmatched" if item["attempts"] >= 2 else "pending"
            error = "Recording lookup or storage unavailable; retry later."
        self.repository.resolved(job, item, state, error, song_id)
