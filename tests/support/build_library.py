"""Explicit test composition using the production service and adapters."""

from lastbridge.features.lastfm_sync.adapters.sqlite.loves import (
    LoveIntentWriter,
)
from lastbridge.features.library.adapters.sqlite.repository import (
    LibraryRepository,
)
from lastbridge.features.library.application.service import (
    Library as LibraryService,
)


def make_library(accounts, catalog, policy):
    return LibraryService(
        LibraryRepository(
            accounts.repository.database, policy, LoveIntentWriter()
        ),
        catalog,
        policy,
    )
