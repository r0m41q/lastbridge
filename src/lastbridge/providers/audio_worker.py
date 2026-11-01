"""Compatibility CLI/module for the moved audio worker."""

import subprocess

from lastbridge.features.playback.adapters.youtube.worker import (
    choose_format,
    download,
    main,
)

__all__ = [
    "choose_format",
    "download",
    "main",
    "subprocess",
]

if __name__ == "__main__":
    main()
