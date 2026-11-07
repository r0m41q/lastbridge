import contextlib

from lastbridge.shared.errors import CatalogError


class AudioProvider:
    def __init__(self, youtube, jumo, settings):
        self.youtube = youtube
        self.jumo = jumo
        self.settings = settings

    async def configure(self, settings):
        self.settings = settings
        self.youtube.timeout = settings.timeout_seconds
        await self.jumo.configure(settings)

    async def fetch(self, source, policy, directory, maximum, reserve):
        if self.settings.provider != "jumo":
            return await self.youtube.fetch(
                source, policy, directory, maximum, reserve
            )
        try:
            return await self.jumo.fetch(
                source, policy, directory, maximum, reserve
            )
        except CatalogError as error:
            if (
                error.message
                in (
                    "Not enough free space for audio",
                    "Recording exceeds the audio file size limit",
                )
                or not self.settings.youtube_fallback
            ):
                raise
            for path in directory.iterdir():
                if path.is_file():
                    with contextlib.suppress(OSError):
                        path.unlink()
            return await self.youtube.fetch(
                source, policy, directory, maximum, reserve
            )
