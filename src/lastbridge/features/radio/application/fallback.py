import asyncio
import logging

from lastbridge.features.radio.application.ports import (
    CatalogAccess,
    TasteProvider,
)
from lastbridge.shared.errors import CatalogError, ProviderError
from lastbridge.shared.lastfm import LastfmRequestError
from lastbridge.shared.recordings import recording_key

logger = logging.getLogger(__name__)
FALLBACK_DEADLINE = 12


class SimilarTracks:
    def __init__(self, catalog: CatalogAccess, remote: TasteProvider):
        self.catalog, self.remote = catalog, remote

    async def fallback(self, source):
        """Public suggestions, strictly matched audio; retain partial work."""
        if not self.remote.configured:
            return []
        seed = self.catalog.song_by_source(source)
        if not seed.get("artist"):
            return []
        songs, seen = [], {recording_key(seed["artist"], seed["title"])}
        try:
            async with asyncio.timeout(FALLBACK_DEADLINE):
                rows = await self.remote.taste_rows(
                    "track.getSimilar",
                    artist=seed["artist"],
                    track=seed["title"],
                    autocorrect="0",
                )
                attempts = 0
                for artist, title in rows:
                    key = recording_key(artist, title)
                    if key in seen:
                        continue
                    seen.add(key)
                    if attempts >= 10:
                        break
                    attempts += 1
                    try:
                        async with asyncio.timeout(2):
                            song = await self.catalog.match_loved(
                                artist, title
                            )
                    except ProviderError, TimeoutError:
                        continue
                    except CatalogError as error:
                        if error.code != 70:
                            break
                        continue
                    songs.append(song)
                    await asyncio.sleep(0.25)
        except LastfmRequestError, TimeoutError:
            # Provider messages, URLs and session secrets stay out of logs.
            logger.debug("Last.fm radio fallback incomplete")
        return songs
