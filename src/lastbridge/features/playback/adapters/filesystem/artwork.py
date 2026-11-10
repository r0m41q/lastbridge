import os
import tempfile
from pathlib import Path

EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
COVER_NAMES = tuple(
    f"{name}.{extension}"
    for name in ("cover", "folder")
    for extension in ("jpg", "jpeg", "png", "webp")
)


class AlbumCovers:
    """Write one atomic album sidecar; preserve existing user artwork."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def audio(self, relative_path):
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root) or path.parent == self.root:
            raise ValueError("Invalid saved audio path")
        return path

    def needed(self, relative_path):
        path = self.audio(relative_path)
        return path.is_file() and not any(
            image.name.casefold() in COVER_NAMES
            for image in path.parent.iterdir()
        )

    def write(self, relative_path, data, media_type):
        if not self.needed(relative_path):
            return False
        if (
            media_type not in EXTENSIONS
            or not 0 < len(data) <= 2 * 1024 * 1024
        ):
            raise ValueError("Invalid artwork")
        valid = (
            media_type == "image/jpeg"
            and data.startswith(b"\xff\xd8\xff")
            or media_type == "image/png"
            and data.startswith(b"\x89PNG\r\n\x1a\n")
            or media_type == "image/webp"
            and data[:4] == b"RIFF"
            and data[8:12] == b"WEBP"
        )
        if not valid:
            raise ValueError("Invalid artwork")
        path = self.audio(relative_path)
        destination = path.parent / f"cover.{EXTENSIONS[media_type]}"
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=".lastbridge-cover-",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            try:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
                if not self.needed(relative_path):
                    return False
                # Linking publishes a complete file without overwriting a
                # cover another process may have supplied concurrently.
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    return False
            finally:
                temporary.unlink(missing_ok=True)
        return True
