from collections.abc import Awaitable, Callable
from typing import Protocol

from lastbridge.features.administration.domain.audio import AudioSettings


class DashboardStore(Protocol):
    def snapshot(self) -> dict: ...
    def user(self, user_id: int) -> dict | None: ...


class AudioSettingsStore(Protocol):
    def snapshot(self) -> AudioSettings: ...

    def save(self, settings: AudioSettings) -> None: ...


AudioConfigurator = Callable[[AudioSettings], Awaitable[None]]


class StorageUsage(Protocol):
    async def snapshot(self) -> dict: ...
