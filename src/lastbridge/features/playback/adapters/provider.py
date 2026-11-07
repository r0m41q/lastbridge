import contextlib
import logging

from lastbridge.shared.errors import CatalogError

logger = logging.getLogger(__name__)


class AudioProvider:
    def __init__(self, youtube, jumo, settings):
        self.youtube = youtube
        self.jumo = jumo
        self.settings = settings

    async def configure(self, settings):
        self.settings = settings
        self.youtube.timeout = settings.timeout_seconds
        await self.jumo.configure(settings)

    def for_role(self, role):
        if role not in ("stream", "download"):
            raise ValueError("Unknown audio acquisition role")
        return _RoleProvider(self, role)

    async def fetch(self, source, policy, directory, maximum, reserve):
        return await self.fetch_for(
            "stream", source, policy, directory, maximum, reserve
        )

    async def fetch_for(
        self, role, source, policy, directory, maximum, reserve
    ):
        provider_name = (
            self.settings.stream_provider
            if role == "stream"
            else self.settings.download_provider
        )
        provider = self.youtube if provider_name == "youtube" else self.jumo
        logger.info(
            "Audio provider request started provider=%s role=%s source=%s "
            "policy=%s",
            provider_name,
            role,
            source,
            policy,
        )
        try:
            result = await provider.fetch(
                source, policy, directory, maximum, reserve
            )
        except CatalogError as error:
            if (
                provider_name != "jumo"
                or error.message
                in (
                    "Not enough free space for audio",
                    "Recording exceeds the audio file size limit",
                )
                or not self.settings.youtube_fallback
            ):
                logger.warning(
                    "Audio provider request failed provider=%s role=%s "
                    "source=%s reason=%s",
                    provider_name,
                    role,
                    source,
                    error.message,
                )
                raise
            logger.warning(
                "Audio provider request failed provider=%s role=%s source=%s "
                "reason=%s; trying provider=youtube",
                provider_name,
                role,
                source,
                error.message,
            )
            for path in directory.iterdir():
                if path.is_file():
                    with contextlib.suppress(OSError):
                        path.unlink()
            result = await self.youtube.fetch(
                source, policy, directory, maximum, reserve
            )
        except Exception as error:
            logger.error(
                "Audio provider request crashed provider=%s role=%s "
                "source=%s error_type=%s",
                provider_name,
                role,
                source,
                type(error).__name__,
            )
            raise
        logger.info(
            "Audio provider request completed provider=%s role=%s source=%s",
            provider_name,
            role,
            source,
        )
        return result


class _RoleProvider:
    def __init__(self, router, role):
        self.router, self.role = router, role

    async def fetch(self, source, policy, directory, maximum, reserve):
        return await self.router.fetch_for(
            self.role, source, policy, directory, maximum, reserve
        )
