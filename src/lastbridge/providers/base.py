"""Compatibility exports for the probe and unmigrated provider callers."""

from pathlib import Path
from typing import Any, Protocol

from lastbridge.features.catalog.domain.models import (
    Candidate,
    Recording,
)
from lastbridge.shared.errors import ProviderError

__all__ = [
    "Candidate",
    "Recording",
    "MusicProvider",
    "ProviderError",
]


class MusicProvider(Protocol):
    def search(self, recording: Recording) -> list[Candidate]: ...

    def inspect(
        self, source_id: str, sample_dir: Path | None = None
    ) -> dict[str, Any]: ...
