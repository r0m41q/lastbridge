"""Compatibility CLI/module for the moved catalog worker."""

from lastbridge.features.catalog.adapters.youtube.worker import (
    audio_row,
    execute,
    main,
    radio_tracks,
)

__all__ = [
    "audio_row",
    "execute",
    "radio_tracks",
    "main",
]

if __name__ == "__main__":
    main()
