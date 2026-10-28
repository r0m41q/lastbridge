from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class Recording:
    artist: str
    title: str
    album: str | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True)
class Candidate:
    source_id: str
    title: str
    artists: tuple[str, ...]
    album: str | None
    duration_seconds: float | None
    catalog_audio: bool


class ProviderError(Exception):
    """Only fixed, public error codes cross the subprocess boundary."""


class MusicProvider(Protocol):
    def search(self, recording: Recording) -> list[Candidate]: ...

    def inspect(
        self, source_id: str, sample_dir: Path | None = None
    ) -> dict[str, Any]: ...
