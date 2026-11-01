"""Explicit test composition using the production service and adapters."""

from lastbridge.features.discovery.adapters.sqlite.repository import (
    MixRepository,
)
from lastbridge.features.discovery.application.service import (
    Mixes as MixesService,
)
from lastbridge.features.discovery.domain.policy import DAY
from lastbridge.providers.lastfm.session import LastfmSession


def make_mixes(
    accounts,
    catalog,
    library,
    lastfm,
    client,
    *,
    refresh_interval=DAY,
    inactive_ttl=7 * DAY,
):
    return MixesService(
        MixRepository(accounts.repository.database),
        catalog,
        library,
        LastfmSession(lastfm, client),
        refresh_interval=refresh_interval,
        inactive_ttl=inactive_ttl,
    )
