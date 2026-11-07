from collections.abc import Awaitable, Callable

from lastbridge.features.administration.application.ports import (
    AudioSettingsStore,
    DashboardStore,
    StorageUsage,
)
from lastbridge.features.administration.domain.accounting import (
    account_storage,
)
from lastbridge.features.administration.domain.audio import (
    MEGABYTE,
    AudioSettings,
)


class Administration:
    def __init__(
        self,
        repository: DashboardStore,
        storage: StorageUsage,
        *,
        downloads: Callable[[int], None],
        mixes: Callable[[int], None],
        sync: Callable[[int, str], None],
        charts: Callable[[str], None],
        audio_settings: AudioSettingsStore,
        configure_audio: Callable[[AudioSettings], Awaitable[None]],
        audio_ceilings: tuple[int, int, int],
    ):
        self.repository, self.storage = repository, storage
        self.downloads, self.mixes = downloads, mixes
        self.sync, self.charts = sync, charts
        self.audio_settings = audio_settings
        self.configure_audio = configure_audio
        self.audio_ceilings = audio_ceilings

    async def dashboard(self):
        usage = await self.storage.snapshot()
        report = self.repository.snapshot()
        totals = account_storage(
            report["users"],
            report.pop("retained"),
            usage.pop("entries"),
            usage.pop("policy"),
        )
        return report | {
            "audio": self.audio_settings.snapshot()
            .within_ceilings(self.audio_ceilings)
            .as_dict(),
            "audio_ceilings": {
                "cache_mb": self.audio_ceilings[0] / MEGABYTE,
                "file_max_mb": self.audio_ceilings[1] / MEGABYTE,
                "free_reserve_mb": self.audio_ceilings[2] / MEGABYTE,
            },
            "storage": usage | totals,
        }

    async def update_audio(self, values):
        previous = self.audio_settings.snapshot().within_ceilings(
            self.audio_ceilings
        )
        settings = AudioSettings.from_form(values, self.audio_ceilings)
        self.audio_settings.save(settings)
        try:
            await self.configure_audio(settings)
        except BaseException:
            self.audio_settings.save(previous)
            raise

    def retry(self, user_id, action):
        user = self.repository.user(user_id)
        if user is None:
            raise ValueError("Account does not exist.")
        if action == "downloads":
            self.downloads(user_id)
        elif action in ("mixes", "resume", "retry-matches", "retry-scrobbles"):
            if user["link_status"] != "linked":
                raise ValueError("The user must relink Last.fm first.")
            if action == "mixes":
                self.mixes(user_id)
            else:
                self.sync(user_id, action)
        elif action == "pause":
            self.sync(user_id, action)
        else:
            raise ValueError("Unknown job action.")
