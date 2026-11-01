"""Explicit test composition using the production service and adapters."""

from lastbridge.features.lyrics.adapters.lrclib.provider import Lrclib
from lastbridge.features.lyrics.adapters.sqlite.repository import (
    LyricsRepository,
)
from lastbridge.features.lyrics.application.service import (
    Lyrics as LyricsService,
)


def make_lyrics(accounts, catalog, client):
    return LyricsService(
        LyricsRepository(accounts.repository.database), catalog, Lrclib(client)
    )
