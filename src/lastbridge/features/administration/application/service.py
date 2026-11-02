from collections.abc import Callable

from lastbridge.features.administration.application.ports import (
    DashboardStore,
    StorageUsage,
)
from lastbridge.features.administration.domain.accounting import (
    account_storage,
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
    ):
        self.repository, self.storage = repository, storage
        self.downloads, self.mixes = downloads, mixes
        self.sync, self.charts = sync, charts

    async def dashboard(self):
        usage = await self.storage.snapshot()
        report = self.repository.snapshot()
        totals = account_storage(
            report["users"],
            report.pop("retained"),
            usage.pop("entries"),
            usage.pop("policy"),
        )
        return report | {"storage": usage | totals}

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
