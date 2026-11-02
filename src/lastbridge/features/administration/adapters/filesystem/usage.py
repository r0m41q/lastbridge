import shutil


class StorageUsage:
    def __init__(self, storage):
        self.storage = storage

    async def snapshot(self):
        async with self.storage.lock:
            disk = shutil.disk_usage(self.storage.directory)
            return {
                "entries": {
                    k: dict(v) for k, v in self.storage.entries.items()
                },
                "policy": self.storage.policy,
                "budget": self.storage.budget,
                "reserved": self.storage.reserved,
                "active": len(self.storage.jobs),
                "disk_total": disk.total,
                "disk_used": disk.used,
                "disk_free": disk.free,
                "low_space": disk.free
                < (
                    self.storage.reserve
                    + self.storage.reserved
                    + self.storage.allowance
                ),
            }
