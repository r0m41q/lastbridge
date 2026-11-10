from lastbridge.features.lastfm_sync.application.ports import (
    SecretCipher,
    SyncProvider,
    SyncStore,
)
from lastbridge.shared.lastfm import LastfmRequestError


class LoveDelivery:
    def __init__(
        self, repository: SyncStore, remote: SyncProvider, cipher: SecretCipher
    ):
        self.repository, self.remote, self.cipher = repository, remote, cipher

    async def deliver_one(self):
        claimed = self.repository.claim_love()
        if claimed is None:
            return False
        job, user = claimed
        if user is None:
            return True
        state, error = "sent", None
        try:
            await self.remote.set_love(
                self.cipher.decrypt(user["lastfm_secret"]), job
            )
        except LastfmRequestError as failure:
            # Setting the desired love state is safe to repeat after an
            # ambiguous response, unlike adding a scrobble to history.
            state = (
                "pending"
                if failure.state in ("pending", "uncertain")
                or failure.code == 8
                else failure.state
            )
            error = (
                "Last.fm love change could not be confirmed; retry scheduled."
                if state == "pending"
                else str(failure)
            )
            if failure.code == 9:
                self.repository.invalidate(user)
        self.repository.complete_love(job, state, error)
        return True
