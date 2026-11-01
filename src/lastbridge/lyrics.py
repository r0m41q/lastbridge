"""Temporary constructor and pure parser exports for existing callers."""

from lastbridge.features.lyrics.adapters.lrclib.provider import (
    Lrclib,
    retry_after,
)
from lastbridge.features.lyrics.adapters.sqlite.repository import (
    LyricsRepository,
)
from lastbridge.features.lyrics.application.service import (
    Lyrics as LyricsService,
)
from lastbridge.features.lyrics.domain.lyrics import structured, timed_lines

__all__ = ["Lyrics", "retry_after", "structured", "timed_lines"]


class Lyrics(LyricsService):
    def __init__(self, accounts, catalog, client):
        super().__init__(
            LyricsRepository(accounts.database), catalog, Lrclib(client)
        )
