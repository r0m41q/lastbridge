import asyncio
import base64
import subprocess
from functools import partial

import httpx
import pytest
from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.primitives.padding import PKCS7

from lastbridge.features.playback.adapters.rhythmax import worker
from lastbridge.features.playback.adapters.rhythmax.client import (
    RhythmaxClient,
    RhythmaxError,
    media_url,
)
from lastbridge.features.playback.adapters.rhythmax.provider import (
    AudioProvider,
)


def encrypt_url(url):
    padder = PKCS7(64).padder()
    raw = padder.update(url.encode()) + padder.finalize()
    cipher = Cipher(TripleDES(b"38346591" * 3), modes.ECB()).encryptor()
    return base64.b64encode(cipher.update(raw) + cipher.finalize()).decode()


def song(track_id="song1", **info):
    return {
        "id": track_id,
        "type": "song",
        "title": "Instant Crush (feat. Julian Casablancas)",
        "more_info": {
            "album": "Random Access Memories",
            "duration": "337",
            "artists": {
                "primary": [
                    {"name": "Daft Punk"},
                    {"name": "Julian Casablancas"},
                ]
            },
            **info,
        },
    }


RECORDING = dict(
    artist="Daft Punk",
    title="Instant Crush",
    album="Random Access Memories",
    duration=337,
)


def test_candidate_matches_saavn_metadata_and_excludes_other_versions():
    rows = [
        song("wrong-album", album="Drumless Edition"),
        song("too-short", duration="30"),
        song(),
    ]
    assert worker.choose_candidate(rows, RECORDING) == "song1"
    assert worker.choose_candidate([song(), song("duplicate")], RECORDING) == (
        "song1"
    )
    assert worker.choose_candidate([song(duration="bad")], RECORDING) is None
    assert (
        worker.choose_candidate(
            [song(artists={"primary": [{"name": "Cover Artist"}]})], RECORDING
        )
        is None
    )
    assert worker.choose_candidate([], RECORDING) is None


def test_candidate_prefers_album_then_duration_then_provider_order():
    rows = [
        song("compilation", album="Hits", duration="337"),
        song("original-longer", duration="340"),
        song("original-first", duration="338"),
        song("original-second", duration="338"),
    ]
    assert worker.choose_candidate(rows, RECORDING) == "original-first"
    assert worker.choose_candidate(rows[:1], RECORDING) == "compilation"
    assert (
        worker.choose_candidate(rows, RECORDING | {"album": "Missing release"})
        == "compilation"
    )
    assert (
        worker.choose_candidate(
            rows, RECORDING | {"album": "Missing release", "duration": None}
        )
        == "compilation"
    )


@pytest.mark.parametrize("explicit", [True, False])
def test_candidate_preserves_explicit_or_clean_version(explicit):
    wrong = song("opposite") | {"isExplicit": not explicit}
    unknown = song("unknown")
    matching = song("matching", album="Hits") | {"isExplicit": explicit}
    recording = RECORDING | {"isExplicit": explicit}
    assert (
        worker.choose_candidate([wrong, unknown, matching], recording)
        == "matching"
    )
    assert worker.choose_candidate([wrong, unknown], recording) is None


def test_unknown_catalog_version_prefers_explicit_then_unknown_then_clean():
    clean = song("clean") | {"isExplicit": False}
    unknown = song("unknown")
    explicit = song("explicit", album="Hits") | {"isExplicit": True}
    assert worker.choose_candidate([clean, unknown, explicit], RECORDING) == (
        "explicit"
    )
    assert worker.choose_candidate([clean, unknown], RECORDING) == "unknown"
    assert worker.choose_candidate([clean], RECORDING) == "clean"


@pytest.mark.parametrize("version", ["Live", "Remix", "Acoustic"])
def test_candidate_keeps_recording_version_qualifiers(version):
    other = song("other") | {"title": f"Instant Crush ({version})"}
    assert worker.choose_candidate([other], RECORDING) is None
    assert (
        worker.choose_candidate(
            [song(), other],
            RECORDING | {"title": f"Instant Crush ({version})"},
        )
        == "other"
    )


@pytest.mark.parametrize(
    "seconds", ["nan", "inf", "-inf", "0", "-1", None, {}, 10**400]
)
def test_candidate_rejects_invalid_duration(seconds):
    assert worker.choose_candidate([song(duration=seconds)], RECORDING) is None


def test_candidate_accepts_combined_primary_credits_and_skips_malformed_rows():
    rows = [
        None,
        song(artists=None),
        song(artists={"primary": None}),
        song() | {"id": None},
        song("valid"),
    ]
    assert (
        worker.choose_candidate(
            rows, RECORDING | {"artist": "Daft Punk, Julian Casablancas"}
        )
        == "valid"
    )


def test_surround_sound_explicit_and_clean_duplicates():
    recording = {
        "artist": "JID",
        "title": "Surround Sound (feat. 21 Savage & Baby Tate)",
        "album": "The Forever Story (Extended Version)",
        "duration": 230,
    }
    rows = [
        {
            "id": track_id,
            "type": "song",
            "title": "Surround Sound",
            "isExplicit": explicit,
            "more_info": {
                "album": recording["album"],
                "duration": "230",
                "artists": {"primary": [{"name": "Jid"}]},
            },
        }
        for track_id, explicit in [("CQ2zbct2", False), ("9KYRUmL_", True)]
    ]
    assert worker.choose_candidate(rows, recording) == "9KYRUmL_"
    assert (
        worker.choose_candidate(rows, recording | {"isExplicit": False})
        == "CQ2zbct2"
    )


@pytest.mark.parametrize("explicit", [True, False, None])
def test_provider_passes_selected_version_to_worker(
    tmp_path, monkeypatch, explicit
):
    async def scenario():
        launch = asyncio.create_subprocess_exec

        async def substitute(*args, **kwargs):
            return await launch(
                args[0],
                "-c",
                "import json,sys; p=json.load(sys.stdin); "
                "print(json.dumps({'policy': p['policy'], "
                "'isExplicit': p['recording']['isExplicit']}))",
                **kwargs,
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", substitute)
        provider = AudioProvider(
            lambda _: RECORDING | {"isExplicit": explicit}
        )
        result = await provider.fetch(
            "khnokW3Mw24", "aac", tmp_path, 1_000_000, 0
        )
        assert result["isExplicit"] is explicit

    asyncio.run(scenario())


def test_media_url_matches_frontend_quality_selection():
    encrypted = encrypt_url(
        "http://aac.saavncdn.com/123/recording_96.mp4?test=1"
    )
    assert (
        media_url(encrypted)
        == "https://aac.saavncdn.com/123/recording_320.mp4?test=1"
    )
    assert media_url(
        encrypted.rstrip("=").replace("/", "_").replace("+", "-")
    ) == media_url(encrypted)


@pytest.mark.parametrize(
    "value",
    [
        "bad!",
        encrypt_url("https://example.com/a_96.mp4"),
        encrypt_url("file:///a_96.mp4"),
        encrypt_url("https://aac.saavncdn.com/a.mp3"),
    ],
)
def test_media_url_rejects_invalid_descriptors(value):
    with pytest.raises(RhythmaxError, match="invalid_media_url"):
        media_url(value)


def test_search_uses_the_frontend_api_without_tidal_tokens():
    def handler(request):
        assert request.url.host == "rthmx.vercel.app"
        assert request.url.path == "/api/songs"
        assert request.url.params["q"] == "Daft Punk Instant Crush"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"results": [song()]})

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            selected = await worker.resolve(RhythmaxClient(http), RECORDING)
            assert selected["id"] == "song1"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,payload,reason",
    [
        (503, {}, "search_unavailable"),
        (200, {"results": {}}, "invalid_search_response"),
    ],
)
def test_search_failure_is_sanitized(status, payload, reason):
    async def scenario():
        transport = httpx.MockTransport(
            lambda _: httpx.Response(status, json=payload)
        )
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(RhythmaxError, match=reason):
                await RhythmaxClient(http).search("test")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "policy,bitrate,seconds,error",
    [
        ("aac", "320k", 3, None),
        ("opus", "320k", 3, None),
        ("aac", "128k", 3, "low_quality"),
        ("aac", "320k", 30, "invalid_audio"),
    ],
)
def test_download_produces_a_complete_valid_file(
    tmp_path, monkeypatch, policy, bitrate, seconds, error
):
    source = tmp_path / "input.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=duration=3:sample_rate=44100:seed=1[a];"
            "anoisesrc=duration=3:sample_rate=44100:seed=2[b];"
            "[a][b]amerge=inputs=2",
            "-ac",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            bitrate,
            str(source),
        ],
        check=True,
    )
    content = source.read_bytes()
    encrypted = encrypt_url("https://aac.saavncdn.com/123/recording_96.mp4")
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "rthmx.vercel.app":
            return httpx.Response(
                200,
                json={
                    "results": [
                        song(
                            duration=str(seconds),
                            encrypted_media_url=encrypted,
                        )
                    ]
                },
            )
        assert request.url.host == "aac.saavncdn.com"
        assert request.url.path.endswith("_320.mp4")
        return httpx.Response(200, content=content)

    monkeypatch.setattr(
        worker.httpx,
        "AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )
    task = worker.download(
        dict(
            recording={**RECORDING, "duration": seconds},
            directory=str(tmp_path),
            policy=policy,
            maximum=1_000_000,
            reserve=0,
        )
    )
    if error:
        with pytest.raises(RhythmaxError, match=error):
            asyncio.run(task)
        assert not (tmp_path / "rhythmax-source.mp4").exists()
        return
    result = asyncio.run(task)
    assert result["policy"] == policy
    assert abs(float(result["ffprobe"]["format"]["duration"]) - 3) < 0.1
    assert result["ffprobe"]["streams"][0]["codec_name"] == policy
    assert not (tmp_path / "rhythmax-source.mp4").exists()
    assert len(calls) == 2


def test_audio_transfer_enforces_size_limit(tmp_path):
    async def scenario():
        transport = httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"x" * 100)
        )
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(RhythmaxError, match="too_large"):
                await RhythmaxClient(http).fetch_audio(
                    encrypt_url("https://aac.saavncdn.com/a_96.mp4"),
                    tmp_path / "audio",
                    50,
                    0,
                )
        assert not (tmp_path / "audio").read_bytes()

    asyncio.run(scenario())
