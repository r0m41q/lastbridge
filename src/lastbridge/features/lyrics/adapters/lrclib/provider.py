import asyncio
import json
import math
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from lastbridge.features.lyrics.domain.errors import (
    CoolingDown,
    LyricsUnavailable,
)
from lastbridge.features.lyrics.domain.lyrics import structured

MAXIMUM = 256 * 1024


def retry_after(value):
    try:
        seconds = float(value)
    except ValueError, TypeError:
        try:
            seconds = (
                parsedate_to_datetime(value) - datetime.now(UTC)
            ).total_seconds()
        except ValueError, TypeError, OverflowError:
            return 60
    return max(60, seconds) if math.isfinite(seconds) else 60


class Lrclib:
    def __init__(self, client):
        self.client = client
        self.lock = asyncio.Lock()
        self.next_request = 0
        self.blocked_until = 0

    async def get(self, song):
        try:
            async with self.lock:
                if self.blocked_until > time.monotonic():
                    raise CoolingDown
                await asyncio.sleep(
                    max(0, self.next_request - time.monotonic())
                )
                try:
                    async with self.client.stream(
                        "GET",
                        "https://lrclib.net/api/get",
                        params={
                            "artist_name": song["artist"],
                            "track_name": song["title"],
                            "album_name": song["album"],
                            "duration": song["duration"],
                        },
                        headers={
                            "User-Agent": "Lastbridge/0.1.0 "
                            "(https://github.com/r0m41q/lastbridge)"
                        },
                        follow_redirects=False,
                    ) as response:
                        if response.status_code in (429, 503):
                            self.blocked_until = time.monotonic() + (
                                retry_after(
                                    response.headers.get("Retry-After")
                                )
                            )
                        if response.status_code == 404:
                            result = []
                        else:
                            response.raise_for_status()
                            output = bytearray()
                            async for chunk in response.aiter_bytes():
                                output.extend(chunk)
                                if len(output) > MAXIMUM:
                                    raise ValueError
                            result = structured(song, json.loads(output))
                finally:
                    self.next_request = time.monotonic() + 0.25
            return result
        except httpx.HTTPError:
            raise LyricsUnavailable from None
