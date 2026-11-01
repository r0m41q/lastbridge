from dataclasses import dataclass


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
