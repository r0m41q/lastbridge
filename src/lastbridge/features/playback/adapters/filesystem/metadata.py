import json
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
        *,
        cover: Path | None = None,
        lyrics: str | None = None,
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
            command = self._command(
                source, temporary_path, descriptor, cover=cover, lyrics=lyrics
            )
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            with temporary_path.open("rb") as completed:
                os.fsync(completed.fileno())
            os.replace(temporary_path, destination)
        except OSError, subprocess.SubprocessError, TimeoutError:
            temporary_path.unlink(missing_ok=True)
            raise RuntimeError("Audio metadata writing failed") from None
        return destination

    @staticmethod
    def _command(
        source: Path,
        destination: Path,
        descriptor: MediaDescriptor,
        *,
        cover: Path | None = None,
        lyrics: str | None = None,
    ) -> list[str]:
        command = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
        ]
        if cover is not None:
            command.extend(["-f", "image2", "-i", str(cover)])
        command.extend(
            [
                "-map",
                "0:a",
                "-map",
                "1:v:0" if cover is not None else "0:v?",
                "-map_metadata",
                "0",
                "-c",
                "copy",
            ]
        )
        if cover is not None:
            command.extend(["-disposition:v:0", "attached_pic"])
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
        if lyrics is not None:
            command.extend(["-metadata", f"lyrics={lyrics}"])
        # The minimal media build enables mp4, but not the ipod muxer that
        # FFmpeg normally selects from a .m4a suffix.
        container = "mp4" if descriptor.policy == "aac" else "webm"
        command.extend(["-f", container, str(destination)])
        return command

    @staticmethod
    def inspect(path, descriptor):
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format_tags:stream=codec_type:"
                    "stream_disposition=attached_pic",
                    "-of",
                    "json",
                    str(path),
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )
            document = json.loads(result.stdout)
        except OSError, subprocess.SubprocessError, ValueError:
            raise RuntimeError("Saved audio inspection failed") from None
        tags = {
            k.casefold(): v
            for k, v in document.get("format", {}).get("tags", {}).items()
        }
        expected = {
            "title": descriptor.title,
            "artist": descriptor.artist,
            "album": descriptor.album,
            "album_artist": descriptor.album_artist,
            "track_number": descriptor.track_number,
            "disc_number": descriptor.disc_number,
            "year": descriptor.year,
            "genre": descriptor.genre,
        }
        matched = all(
            tags.get(TAGS[descriptor.policy][field].casefold()) == str(value)
            for field, value in expected.items()
            if value is not None and value != ""
        )
        if not any(
            s.get("codec_type") == "audio" for s in document.get("streams", [])
        ):
            raise RuntimeError("Saved audio inspection failed")
        return {
            "tags": "present" if matched else "needs repair",
            "embedded_cover": any(
                s.get("disposition", {}).get("attached_pic") == 1
                for s in document.get("streams", [])
            ),
            "embedded_lyrics": bool(tags.get("lyrics")),
        }
