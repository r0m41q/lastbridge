import shutil
from pathlib import Path

from lastbridge.features.playback.adapters.filesystem.storage import AudioFiles
from lastbridge.features.playback.adapters.http.response import (
    AudioResponse,
)
from lastbridge.features.playback.adapters.youtube.provider import (
    AudioProvider,
)
from lastbridge.features.playback.application.service import (
    AudioCache as Service,
)

__all__ = [
    "AudioCache",
    "AudioProvider",
    "AudioResponse",
    "shutil",
]


class AudioCache(Service):
    def __init__(
        self,
        directory: Path,
        budget=1_000_000_000,
        maximum=64 * 1024 * 1024,
        reserve=256 * 1024 * 1024,
        policy="aac",
        provider=None,
        saved_directory=None,
        pins=(),
    ):
        storage = AudioFiles(
            directory, budget, maximum, reserve, policy, saved_directory, pins
        )
        super().__init__(
            storage,
            provider if provider is not None else AudioProvider(),
            AudioResponse,
        )

    @property
    def pins(self):
        return self.storage.pins

    @pins.setter
    def pins(self, value):
        self.storage.pins = value

    @property
    def lock(self):
        return self.storage.lock

    @lock.setter
    def lock(self, value):
        self.storage.lock = value

    @property
    def leases(self):
        return self.storage.leases

    @leases.setter
    def leases(self, value):
        self.storage.leases = value

    @property
    def closed(self):
        return self.storage.closed

    @closed.setter
    def closed(self, value):
        self.storage.closed = value

    @property
    def jobs(self):
        return self.storage.jobs

    @jobs.setter
    def jobs(self, value):
        self.storage.jobs = value

    @property
    def policy(self):
        return self.storage.policy

    @policy.setter
    def policy(self, value):
        self.storage.policy = value

    @property
    def directory(self):
        return self.storage.directory

    @directory.setter
    def directory(self, value):
        self.storage.directory = value

    @property
    def reserve(self):
        return self.storage.reserve

    @reserve.setter
    def reserve(self, value):
        self.storage.reserve = value

    @property
    def reserved(self):
        return self.storage.reserved

    @reserved.setter
    def reserved(self, value):
        self.storage.reserved = value

    @property
    def allowance(self):
        return self.storage.allowance

    @allowance.setter
    def allowance(self, value):
        self.storage.allowance = value

    @property
    def incoming(self):
        return self.storage.incoming

    @incoming.setter
    def incoming(self, value):
        self.storage.incoming = value

    @property
    def maximum(self):
        return self.storage.maximum

    @maximum.setter
    def maximum(self, value):
        self.storage.maximum = value

    @property
    def owner(self):
        return self.storage.owner

    @owner.setter
    def owner(self, value):
        self.storage.owner = value

    @property
    def budget(self):
        return self.storage.budget

    @budget.setter
    def budget(self, value):
        self.storage.budget = value

    @property
    def saved_directory(self):
        return self.storage.saved_directory

    @saved_directory.setter
    def saved_directory(self, value):
        self.storage.saved_directory = value

    def restore(self, *args, **kwargs):
        return self.storage.restore(*args, **kwargs)

    def cleanup_aliases(self, *args, **kwargs):
        return self.storage.cleanup_aliases(*args, **kwargs)

    def trim(self, *args, **kwargs):
        return self.storage.trim(*args, **kwargs)

    def relocate(self, *args, **kwargs):
        return self.storage.relocate(*args, **kwargs)
