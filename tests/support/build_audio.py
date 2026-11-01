"""Explicit test composition using the production service and adapters."""

from pathlib import Path

from lastbridge.features.playback.adapters.filesystem.storage import AudioFiles
from lastbridge.features.playback.adapters.http.response import AudioResponse
from lastbridge.features.playback.adapters.youtube.provider import (
    AudioProvider,
)
from lastbridge.features.playback.application.service import (
    AudioCache as AudioCacheService,
)


def make_audio(
    directory: Path,
    budget=1000000000,
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
    return AudioCacheService(
        storage,
        provider if provider is not None else AudioProvider(),
        AudioResponse,
    )
