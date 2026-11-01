"""Explicit test composition using the production service and adapters."""

from lastbridge.features.radio.adapters.catalog import RadioCatalog
from lastbridge.features.radio.adapters.sqlite.repository import (
    RadioRepository,
)
from lastbridge.features.radio.application.service import Radio as RadioService
from lastbridge.providers.lastfm.session import LastfmSession


def make_radio(accounts, catalog, library, lastfm=None, client=None):
    return RadioService(
        RadioRepository(accounts.repository.database),
        RadioCatalog(catalog),
        library,
        LastfmSession(lastfm, client),
    )
