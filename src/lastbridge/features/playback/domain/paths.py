import hashlib
import re
import unicodedata
from collections.abc import Iterable
from pathlib import PurePosixPath

from lastbridge.features.playback.domain.models import (
    RENDITIONS,
    MediaDescriptor,
)

MAX_COMPONENT_LENGTH = 96
RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE = re.compile(r"\s+")


def safe_component(value: str | None, fallback: str) -> str:
    """Return one bounded, filesystem-safe, human-readable path component."""

    text = unicodedata.normalize("NFKC", value or "")
    text = "".join(char if char.isprintable() else " " for char in text)
    text = UNSAFE.sub("-", text)
    text = WHITESPACE.sub(" ", text).strip(" .")
    if not text:
        text = fallback
    if text.casefold() in RESERVED_NAMES:
        text = f"_{text}"
    if len(text) > MAX_COMPONENT_LENGTH:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
        text = (
            text[: MAX_COMPONENT_LENGTH - len(digest) - 1].rstrip(" .")
            + "~"
            + digest
        )
    return text


def _filename(base: str, extension: str) -> str:
    filename = f"{base}.{extension}"
    if len(filename) <= MAX_COMPONENT_LENGTH:
        return filename
    digest = hashlib.sha256(filename.encode("utf-8")).hexdigest()[:10]
    keep = MAX_COMPONENT_LENGTH - len(extension) - len(digest) - 2
    return f"{base[:keep].rstrip(' .')}~{digest}.{extension}"


def saved_relative_path(
    descriptor: MediaDescriptor,
    occupied: Iterable[str] = (),
) -> str:
    """Allocate a readable saved path without escaping the saved root.

    The policy is part of the base name so multiple renditions never collide.
    If another logical source already owns the readable name, the source ID is
    added as a stable disambiguator.
    """

    if descriptor.policy not in RENDITIONS:
        raise ValueError("Unknown audio policy")
    artist = safe_component(descriptor.artist, "Unknown Artist")
    album = safe_component(descriptor.album, "Unknown Album")
    title = safe_component(descriptor.title, descriptor.source)
    extension = RENDITIONS[descriptor.policy][0]
    prefix = (
        f"{descriptor.track_number:02d} - "
        if isinstance(descriptor.track_number, int)
        and not isinstance(descriptor.track_number, bool)
        and descriptor.track_number > 0
        else ""
    )
    base = f"{prefix}{title} [{descriptor.policy}]"
    candidate = PurePosixPath(artist, album, _filename(base, extension))
    occupied_keys = {path.casefold() for path in occupied}
    if candidate.as_posix().casefold() not in occupied_keys:
        return candidate.as_posix()

    source = safe_component(descriptor.source, "source")
    stem = f"{base} ({source})"
    candidate = PurePosixPath(artist, album, _filename(stem, extension))
    suffix = 2
    while candidate.as_posix().casefold() in occupied_keys:
        candidate = PurePosixPath(
            artist, album, _filename(f"{stem} ~{suffix}", extension)
        )
        suffix += 1
    return candidate.as_posix()
