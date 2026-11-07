import base64
import gzip
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def decode_base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: str


class JumoClient:
    SEARCH_PATH = "/qggybiqvrmvapbsrfrlg"
    FETCH_PATH = "/wydmcqpplrfjrstcj"
    ALBUM_PATH = "/xtkbpqjnrrhspkqgjp"
    REGIONS_PATH = "/symdofufvpjpgkmru"

    def __init__(self, http: httpx.AsyncClient, base_url: str):
        self.http = http
        self.base_url = base_url.rstrip("/") + "/"
        self.session = secrets.token_hex(16)
        self.started = time.monotonic()
        self.sequence = 0
        self.private_key = ec.generate_private_key(ec.SECP256R1())

    def url(self, value: str) -> str:
        return (
            value
            if value.startswith("http")
            else urljoin(self.base_url, value)
        )

    def public_key(self) -> str:
        numbers = self.private_key.public_key().public_numbers()
        raw = (
            b"\x04"
            + numbers.x.to_bytes(32, "big")
            + numbers.y.to_bytes(32, "big")
        )
        return base64url(raw)

    def headers(self, path: str, nonce: str, sequence: int) -> dict[str, str]:
        page_age = max(30_000, int((time.monotonic() - self.started) * 1000))
        interaction = {
            "v": 1,
            "nonce": nonce,
            "method": "GET",
            "path": path,
            "pageAgeMs": page_age,
            "lastInputAgeMs": None,
            "pointer": 0,
            "key": 0,
            "scroll": 0,
            "touch": 0,
            "focused": True,
            "visibility": "visible",
            "botSignals": [],
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0.0.0 Safari/537.36"
            ),
            "X-Jumo-Client": "browser",
            "X-Jumo-Session": self.session,
            "X-Jumo-Request-Nonce": nonce,
            "X-Jumo-Sequence": str(sequence),
            "X-Jumo-Interaction": base64url(
                json.dumps(interaction, separators=(",", ":")).encode()
            ),
        }
        if path in (self.SEARCH_PATH, self.FETCH_PATH):
            headers["X-Jumo-Public-Key"] = self.public_key()
            if path == self.SEARCH_PATH:
                headers["X-Jumo-Accept-Encoding"] = "gzip"
        return headers

    def decrypt(
        self, response: httpx.Response, body: bytes, href: str, nonce: str
    ) -> bytes:
        server_key = response.headers.get("X-Jumo-Public-Key")
        if not server_key:
            raise ValueError("Jumo response has no public key")
        raw = decode_base64url(server_key)
        if len(raw) != 65 or raw[0] != 4:
            raise ValueError("Invalid Jumo public key")
        public = ec.EllipticCurvePublicNumbers(
            int.from_bytes(raw[1:33], "big"),
            int.from_bytes(raw[33:], "big"),
            ec.SECP256R1(),
        ).public_key()
        secret = self.private_key.exchange(ec.ECDH(), public)
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"jumo-response-v1",
            info=b"fetch-json",
        ).derive(secret)
        if len(body) < 28:
            raise ValueError("Jumo encrypted response is too short")
        plaintext = AESGCM(key).decrypt(
            body[:12],
            body[12:-16] + body[-16:],
            (href + "\n" + nonce).encode(),
        )
        if response.headers.get("X-Jumo-Encoding") == "gzip":
            plaintext = gzip.decompress(plaintext)
        return plaintext

    async def get(self, value: str) -> ApiResponse:
        url = self.url(value)
        path = urlsplit(url).path
        for attempt in range(6):
            self.sequence += 1
            nonce = secrets.token_hex(12)
            response = await self.http.get(
                url, headers=self.headers(path, nonce, self.sequence)
            )
            body = response.content
            if response.headers.get("X-Jumo-Payload") == "1":
                body = self.decrypt(response, body, url, nonce)
            if response.status_code not in (429, 503) or attempt == 5:
                return ApiResponse(
                    response.status_code, body.decode("utf-8", "replace")
                )
            retry_after = response.headers.get("Retry-After")
            try:
                delay = max(1, min(20, int(retry_after or 2**attempt)))
            except ValueError:
                delay = min(20, 2**attempt)
            await __import__("asyncio").sleep(delay)
        raise AssertionError("unreachable")
