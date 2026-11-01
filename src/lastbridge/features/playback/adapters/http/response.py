import anyio
from starlette.responses import FileResponse

from lastbridge.features.playback.domain.models import RENDITIONS


class AudioResponse(FileResponse):
    def __init__(self, cache, key, entry, download=False):
        extension, media_type = RENDITIONS[entry["policy"]]
        super().__init__(
            cache.path(key, entry),
            media_type=media_type,
            filename=f"{key}.{extension}",
            content_disposition_type="attachment" if download else "inline",
        )
        self.cache, self.key = cache, key

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # ASGI disconnect/cancellation must release the active-file lease.
            with anyio.CancelScope(shield=True):
                await self.cache.release(self.key)
