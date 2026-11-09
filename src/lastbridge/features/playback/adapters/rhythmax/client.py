import base64
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.primitives.padding import PKCS7

API_BASE_URL = "https://rthmx.vercel.app"


class RhythmaxError(RuntimeError):
    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def media_url(encrypted: str) -> str:
    try:
        encoded = (
            "".join(encrypted.split()).replace("-", "+").replace("_", "/")
        )
        raw = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), validate=True
        )
        # Public URL-obfuscation key; verified against the deployed frontend.
        decryptor = Cipher(TripleDES(b"38346591" * 3), modes.ECB()).decryptor()
        padded = decryptor.update(raw) + decryptor.finalize()
        unpadder = PKCS7(64).unpadder()
        url = (unpadder.update(padded) + unpadder.finalize()).decode()
    except (ValueError, UnicodeError) as error:
        raise RhythmaxError("invalid_media_url") from error
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or not parsed.hostname.endswith(".saavncdn.com")
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 80, 443)
        or not re.search(r"_\d+\.mp4$", parsed.path)
    ):
        raise RhythmaxError("invalid_media_url")
    path = re.sub(r"_\d+\.mp4$", "_320.mp4", parsed.path)
    return parsed._replace(scheme="https", path=path).geturl()


class RhythmaxClient:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def search(self, query: str) -> list[dict]:
        try:
            response = await self.http.get(
                f"{API_BASE_URL}/api/songs", params={"q": query}
            )
        except httpx.HTTPError as error:
            raise RhythmaxError("search_unavailable") from error
        if response.status_code != 200:
            raise RhythmaxError("search_unavailable", response.status_code)
        try:
            payload = response.json()
            rows = (
                payload.get("results") if isinstance(payload, dict) else None
            )
            if not isinstance(rows, list):
                raise ValueError("invalid results")
        except ValueError as error:
            raise RhythmaxError("invalid_search_response") from error
        return rows

    async def fetch_audio(
        self, encrypted: str, path: Path, maximum: int, reserve: int
    ):
        import shutil

        try:
            async with self.http.stream(
                "GET", media_url(encrypted)
            ) as response:
                if response.status_code != 200:
                    raise RhythmaxError(
                        "audio_unavailable", response.status_code
                    )
                size = 0
                with path.open("wb") as output:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        size += len(chunk)
                        if size > maximum:
                            raise RhythmaxError("too_large")
                        if shutil.disk_usage(path.parent).free < reserve + len(
                            chunk
                        ):
                            raise RhythmaxError("storage_full")
                        output.write(chunk)
                if not size:
                    raise RhythmaxError("invalid_audio")
        except httpx.HTTPError as error:
            raise RhythmaxError("audio_unavailable") from error
