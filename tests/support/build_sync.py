"""Explicit test composition using the production service and adapters."""

from lastbridge.features.lastfm_sync.adapters.sqlite.repository import (
    SyncRepository,
)
from lastbridge.features.lastfm_sync.application.service import (
    LastfmSync as LastfmSyncService,
)
from lastbridge.providers.lastfm.session import LastfmSession


def make_sync(accounts, catalog, library, lastfm, client):
    return LastfmSyncService(
        SyncRepository(accounts.repository.database),
        catalog,
        library,
        LastfmSession(lastfm, client),
        accounts,
    )
