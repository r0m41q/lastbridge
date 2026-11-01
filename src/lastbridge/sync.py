"""Temporary constructor for the former flat synchronization module."""

from lastbridge.features.lastfm_sync.adapters.sqlite.repository import (
    SyncRepository,
)
from lastbridge.features.lastfm_sync.application.service import (
    LastfmSync as SyncService,
)
from lastbridge.providers.lastfm.session import LastfmSession


class LastfmSync(SyncService):
    def __init__(self, accounts, catalog, library, lastfm, client):
        super().__init__(
            SyncRepository(accounts.database),
            catalog,
            library,
            LastfmSession(lastfm, client),
            accounts,
        )
