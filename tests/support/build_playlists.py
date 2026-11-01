"""Explicit test composition using the production service and adapters."""

from lastbridge.features.playlists.adapters.sqlite.repository import (
    PlaylistRepository,
)
from lastbridge.features.playlists.application.service import (
    Playlists as PlaylistsService,
)


def make_playlists(accounts, catalog, library, mixes, radio):
    return PlaylistsService(
        PlaylistRepository(accounts.repository.database, library.repository),
        catalog,
        library,
        mixes,
        radio,
    )
