from typing import Protocol


class DashboardStore(Protocol):
    def snapshot(self) -> dict: ...
    def user(self, user_id: int) -> dict | None: ...


class StorageUsage(Protocol):
    async def snapshot(self) -> dict: ...
