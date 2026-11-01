from lastbridge.features.lastfm_sync.application.ports import (
    SecretCipher,
    SyncProvider,
    SyncStore,
)
from lastbridge.shared.lastfm import LastfmRequestError


class ScrobbleDelivery:
    def __init__(
        self, repository: SyncStore, remote: SyncProvider, cipher: SecretCipher
    ):
        self.repository, self.remote, self.cipher = repository, remote, cipher

    async def deliver_one(self):
        claimed = self.repository.claim_delivery()
        if claimed is None:
            return False
        job, user = claimed
        if user is None:
            return True
        try:
            state, error = await self.remote.scrobble(
                self.cipher.decrypt(user["lastfm_secret"]), job
            )
        except LastfmRequestError as failure:
            state, error = failure.state, str(failure)
            if failure.code == 9:
                self.repository.invalidate(user)
        self.repository.complete_delivery(job, state, error)
        return True
