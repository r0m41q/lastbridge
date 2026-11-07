import re
from dataclasses import dataclass
from typing import Literal

RENDITIONS = {"aac": ("m4a", "audio/mp4"), "opus": ("webm", "audio/webm")}
KEY = re.compile(r"[A-Za-z0-9_-]{11}-(?:aac|opus)\Z")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")

MediaFileState = Literal[
    "pending", "writing", "ready", "missing", "deleting"
]
MEDIA_FILE_STATES = frozenset(
    {"pending", "writing", "ready", "missing", "deleting"}
)


@dataclass(frozen=True)
class MediaDescriptor:
    """Logical song metadata needed when materializing a media file."""

    source: str
    policy: str
    title: str
    artist: str
    album: str | None = None
    album_artist: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    year: int | None = None
    genre: str | None = None
    artwork_url: str | None = None


@dataclass(frozen=True)
class MediaFile:
    """Durable placement and lifecycle state for one source rendition."""

    source: str
    policy: str
    relative_path: str | None
    container: str
    size: int | None
    checksum: str | None
    provider_identity: str | None
    state: MediaFileState
    created: float
    updated: float
