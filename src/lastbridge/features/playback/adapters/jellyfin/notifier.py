import logging

import httpx

logger = logging.getLogger(__name__)


class JellyfinScanNotifier:
    """Request a Jellyfin library scan without making it a save dependency."""

    def __init__(
        self,
        scan_url: str,
        api_key: str,
        timeout: float = 5,
        client: httpx.AsyncClient | None = None,
    ):
        self.scan_url = scan_url
        self.api_key = api_key
        self.timeout = timeout
        self.client = client or httpx.AsyncClient(timeout=timeout)

    async def notify(self):
        try:
            response = await self.client.post(
                self.scan_url,
                headers={"X-Emby-Token": self.api_key},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            # Jellyfin is an optional index; saved audio is already committed.
            logger.warning("Optional Jellyfin library scan request failed")

    async def close(self):
        await self.client.aclose()
