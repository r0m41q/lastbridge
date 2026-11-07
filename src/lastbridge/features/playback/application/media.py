from lastbridge.features.playback.application.ports import MediaFiles
from lastbridge.features.playback.domain.models import (
    MediaDescriptor,
    MediaFile,
)
from lastbridge.features.playback.domain.paths import saved_relative_path


class MediaPlacement:
    """Assign and persist a stable saved path for a logical rendition."""

    def __init__(self, repository: MediaFiles):
        self.repository = repository

    def ensure(self, descriptor: MediaDescriptor) -> MediaFile:
        current = self.repository.get(descriptor.source, descriptor.policy)
        if current is not None and current.relative_path is not None:
            return current
        occupied = {
            record.relative_path
            for record in self.repository.list()
            if record.relative_path is not None
        }
        relative_path = saved_relative_path(descriptor, occupied)
        return self.repository.ensure(descriptor, relative_path)
