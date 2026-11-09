import asyncio
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from lastbridge.features.administration.domain.audio import AudioSettings
from lastbridge.features.playback.adapters.jumo.client import (
    JumoClient,
    base64url,
)
from lastbridge.features.playback.adapters.jumo.container import decrypt_frame
from lastbridge.features.playback.adapters.jumo.worker import choose_candidate


def test_jumo_frame_cipher_is_reversible():
    key = b"0123456789abcdef"
    plaintext = b"a complete audio frame"

    encrypted = decrypt_frame(key, b"12345678", plaintext)

    assert decrypt_frame(key, b"12345678", encrypted) == plaintext


def test_jumo_candidate_requires_exact_recording_metadata():
    rows = [
        {
            "id": 20,
            "title": "Instant Crush (feat. Julian Casablancas)",
            "performer": {"name": "Daft Punk"},
            "album": {"id": 10, "title": "Random Access Memories"},
            "duration": 337,
        },
        {
            "id": 21,
            "title": "Instant Crush",
            "performer": {"name": "Daft Punk"},
            "album": {"id": 11, "title": "Different release"},
            "duration": 337,
        },
    ]

    assert choose_candidate(
        rows,
        {
            "artist": "Daft Punk",
            "title": "Instant Crush (feat. Julian Casablancas)",
            "album": "Random Access Memories",
            "duration": 337,
        },
    ) == ("10", "20")


def test_jumo_multiple_matches_return_album_and_track_for_closest_duration():
    recording = {
        "artist": "Daft Punk",
        "title": "Instant Crush",
        "album": "Random Access Memories",
        "duration": 337,
    }
    rows = [
        {
            "id": 20,
            "title": "Instant Crush",
            "performer": {"name": "Daft Punk"},
            "album": {"id": 10, "title": "Random Access Memories"},
            "duration": 340,
        },
        {
            "id": 21,
            "title": "Instant Crush",
            "performer": {"name": "Daft Punk"},
            "album": {"id": 11, "title": "Random Access Memories"},
            "duration": 337,
        },
    ]

    album_id, track_id = choose_candidate(rows, recording)

    assert (album_id, track_id) == ("11", "21")
    assert choose_candidate(list(reversed(rows)), recording) == ("11", "21")
    assert choose_candidate([], recording) is None


def test_jumo_response_decryption_matches_protocol():
    async def scenario():
        async with httpx.AsyncClient() as http:
            client = JumoClient(http, "https://jumo-dl.pages.dev")
            server = ec.generate_private_key(ec.SECP256R1())
            public = server.public_key().public_numbers()
            server_key = (
                b"\x04"
                + public.x.to_bytes(32, "big")
                + public.y.to_bytes(32, "big")
            )
            secret = server.exchange(
                ec.ECDH(), client.private_key.public_key()
            )
            key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b"jumo-response-v1",
                info=b"fetch-json",
            ).derive(secret)
            url = "https://jumo-dl.pages.dev/qggybiqvrmvapbsrfrlg?query=x"
            nonce = "0123456789abcdef0123456789abcdef"
            response_nonce = b"123456789012"
            body = response_nonce + AESGCM(key).encrypt(
                response_nonce,
                json.dumps({"tracks": {"items": []}}).encode(),
                (url + "\n" + nonce).encode(),
            )
            response = httpx.Response(
                200,
                headers={
                    "X-Jumo-Public-Key": base64url(server_key),
                },
            )

            assert (
                json.loads(
                    client.decrypt(response, body, url, nonce).decode()
                )["tracks"]["items"]
                == []
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["youtube", "jumo", "rhythmax"])
def test_jumo_settings_are_valid_for_both_provider_choices(provider):
    assert AudioSettings(provider=provider).provider == provider
