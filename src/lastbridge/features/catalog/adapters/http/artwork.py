import httpx

from lastbridge.features.catalog.domain.documents import image_url
from lastbridge.shared.errors import CatalogError


class ArtworkLookup:
    def __init__(self, repository):
        self.repository = repository

    async def fetch(self, client: httpx.AsyncClient, item_id):
        stored = self.repository.read(item_id)
        url = image_url(stored["image"])
        if url is None:
            # Song search may have only its album's image.
            parent = stored["document"].get("albumId")
            if parent:
                return await self.fetch(client, parent)
            raise CatalogError(70, "Artwork unavailable")
        try:
            async with client.stream("GET", url, follow_redirects=False) as r:
                r.raise_for_status()
                media_type = r.headers.get("content-type", "").split(";")[0]
                if media_type not in ("image/jpeg", "image/png", "image/webp"):
                    raise CatalogError(70, "Artwork unavailable")
                data = bytearray()
                async for chunk in r.aiter_bytes(chunk_size=64 * 1024):
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024:
                        raise CatalogError(0, "Artwork exceeds size limit")
                if not data:
                    raise CatalogError(70, "Artwork unavailable")
                return bytes(data), media_type
        except httpx.HTTPError:
            raise CatalogError(0, "Artwork provider unavailable") from None
