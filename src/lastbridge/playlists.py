"""Temporary constructor; explicit wiring moves to bootstrap in R4."""

from lastbridge.features.playlists.adapters.sqlite.repository import (
    PlaylistRepository,
)
from lastbridge.features.playlists.application.service import (
    Playlists as PlaylistService,
)


class Playlists(PlaylistService):
    def __init__(self, accounts, catalog, library, mixes, radio):
        super().__init__(
            PlaylistRepository(accounts.database, library.repository),
            catalog,
            library,
            mixes,
            radio,
        )
