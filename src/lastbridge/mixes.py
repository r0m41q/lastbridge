"""Temporary constructor and public policy imports for existing callers."""

from lastbridge.features.discovery.adapters.sqlite.repository import (
    MixRepository,
)
from lastbridge.features.discovery.application.service import (
    Mixes as MixService,
)
from lastbridge.features.discovery.domain.policy import DAY
from lastbridge.providers.lastfm.session import LastfmSession
from lastbridge.shared.dates import date

__all__ = ["Mixes", "DAY", "date"]


class Mixes(MixService):
    def __init__(
        self,
        accounts,
        catalog,
        library,
        lastfm,
        client,
        *,
        refresh_interval=DAY,
        inactive_ttl=7 * DAY,
    ):
        super().__init__(
            MixRepository(accounts.database),
            catalog,
            library,
            LastfmSession(lastfm, client),
            refresh_interval=refresh_interval,
            inactive_ttl=inactive_ttl,
        )
