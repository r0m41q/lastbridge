"""Temporary constructor for callers of the former flat radio module."""

from lastbridge.features.radio.adapters.catalog import RadioCatalog
from lastbridge.features.radio.adapters.sqlite.repository import (
    RadioRepository,
)
from lastbridge.features.radio.application.service import Radio as RadioService
from lastbridge.providers.lastfm.session import LastfmSession


class Radio(RadioService):
    def __init__(self, accounts, catalog, library, lastfm=None, client=None):
        super().__init__(
            RadioRepository(accounts.database),
            RadioCatalog(catalog),
            library,
            LastfmSession(lastfm, client),
        )
