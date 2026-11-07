import os
import subprocess
import tempfile
from pathlib import Path

from lastbridge.features.playback.domain.models import (
    RENDITIONS,
    MediaDescriptor,
)

TAGS = {
    "aac": {
        "title": "title",
        "artist": "artist",
        "album": "album",
        "album_artist": "album_artist",
        "track_number": "track",
        "disc_number": "disc",
        "year": "date",
        "genre": "genre",
    },
    "opus": {
        "title": "TITLE",
        "artist": "ARTIST",
        "album": "ALBUM",
        "album_artist": "ALBUMARTIST",
        "track_number": "TRACKNUMBER",
        "disc_number": "DISCNUMBER",
        "year": "DATE",
        "genre": "GENRE",
    },
}


class MetadataWriter:
    """Write catalog tags while copying audio streams without transcoding."""

    def write(
        self,
        source: Path,
        destination: Path,
        descriptor: MediaDescriptor,
    ) -> Path:
        if descriptor.policy not in RENDITIONS:
            raise ValueError("Unknown audio policy")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=destination.suffix,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            command = self._command(source, temporary_path, descriptor)
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            os.replace(temporary_path, destination)
        except (OSError, subprocess.SubprocessError, TimeoutError):
            temporary_path.unlink(missing_ok=True)
            raise RuntimeError("Audio metadata writing failed") from None
        return destination

    @staticmethod
    def _command(
        source: Path, destination: Path, descriptor: MediaDescriptor
    ) -> list[str]:
        command = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            "0",
            "-map_metadata",
            "0",
            "-c",
            "copy",
        ]
        values = {
            "title": descriptor.title,
            "artist": descriptor.artist,
            "album": descriptor.album,
            "album_artist": descriptor.album_artist,
            "track_number": descriptor.track_number,
            "disc_number": descriptor.disc_number,
            "year": descriptor.year,
            "genre": descriptor.genre,
        }
        for field, value in values.items():
            if value is not None and value != "":
                command.extend(
                    [
                        "-metadata",
                        f"{TAGS[descriptor.policy][field]}={value}",
                    ]
                )
        command.append(str(destination))
        return command
