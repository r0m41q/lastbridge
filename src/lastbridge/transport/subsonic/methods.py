"""Assemble the feature-owned method registrations."""

from lastbridge.features.accounts.adapters.subsonic.handlers import (
    HANDLERS as accounts,
)
from lastbridge.features.catalog.adapters.subsonic.handlers import (
    HANDLERS as catalog,
)
from lastbridge.features.lastfm_sync.adapters.subsonic.handlers import (
    HANDLERS as lastfm_sync,
)
from lastbridge.features.library.adapters.subsonic.albums import (
    HANDLERS as library_albums,
)
from lastbridge.features.library.adapters.subsonic.handlers import (
    HANDLERS as library,
)
from lastbridge.features.lyrics.adapters.subsonic.handlers import (
    HANDLERS as lyrics,
)
from lastbridge.features.playback.adapters.subsonic.handlers import (
    HANDLERS as playback,
)
from lastbridge.features.playlists.adapters.subsonic.handlers import (
    HANDLERS as playlists,
)
from lastbridge.features.radio.adapters.subsonic.handlers import (
    HANDLERS as radio,
)
from lastbridge.features.radio.adapters.subsonic.stations import (
    HANDLERS as stations,
)

RADIO_STATION_METHODS = frozenset(stations)

HANDLERS = {
    **catalog,
    **lyrics,
    **lastfm_sync,
    **radio,
    **stations,
    **library,
    **accounts,
    **playlists,
    **playback,
    **library_albums,
}
