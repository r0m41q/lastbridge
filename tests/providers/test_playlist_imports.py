import asyncio
import copy
import sys
from types import SimpleNamespace

import pytest
from support.catalog import SONG

from lastbridge.features.catalog.adapters.youtube.worker import execute
from lastbridge.features.playlists.adapters.youtube.imports import (
    PlaylistProvider,
)
from lastbridge.shared.errors import CatalogError, ProviderError


def test_playlist_worker_requests_pagination_and_strips_session_fields(
    monkeypatch,
):
    calls = []

    def playlist(source, *, limit):
        calls.append((source, limit))
        return {
            "title": "Chart",
            "trackCount": 150,
            "tracks": [
                SONG
                | {
                    "title": str(i),
                    "setVideoId": "secret",
                    "feedbackTokens": {"add": "secret"},
                }
                for i in range(150)
            ],
        }

    monkeypatch.setitem(
        sys.modules,
        "ytmusicapi",
        SimpleNamespace(
            YTMusic=lambda: SimpleNamespace(get_playlist=playlist),
        ),
    )
    result = execute(
        {"operation": "playlist", "source": "PL_test_playlist123"}
    )
    assert calls == [("PL_test_playlist123", 1001)]
    assert [row["title"] for row in result["tracks"]] == [
        str(i) for i in range(150)
    ]
    assert "secret" not in str(result)


def test_community_video_requires_exact_audio_match_and_reports_ambiguity():
    raw = [
        SONG,
        SONG | {"videoType": "MUSIC_VIDEO_TYPE_OMV"},
        SONG | {"title": "Different", "videoType": "MUSIC_VIDEO_TYPE_OMV"},
        SONG | {"isAvailable": False},
    ]
    looked_up, stored = [], []

    async def fetch(payload):
        return {"title": "Chart", "tracks": copy.deepcopy(raw)}

    def song(row):
        assert "secret" not in str(row)
        if row["videoType"] == "MUSIC_VIDEO_TYPE_ATV":
            stored.append(row)
            return {"id": "direct-audio"}

    async def match(artist, title):
        looked_up.append((artist, title))
        if title == "Different":
            raise ProviderError("ambiguous_match")
        return {"id": "matched-audio"}

    provider = PlaylistProvider(fetch, song, match)
    snapshot = asyncio.run(provider.load("PL_test_playlist123"))
    assert [e["song_id"] for e in snapshot.entries] == [
        "direct-audio",
        "matched-audio",
        None,
        None,
    ]
    assert snapshot.entries[2]["reason"] == "No exact audio match"
    assert len(stored) == 1 and len(looked_up) == 2


def test_fallback_searches_are_bounded_and_duplicates_reuse_match():
    calls = []

    async def fetch(payload):
        tracks = [
            SONG | {"title": str(i), "videoType": "MUSIC_VIDEO_TYPE_OMV"}
            for i in range(51)
        ]
        return {"title": "Chart", "tracks": tracks + tracks[:1]}

    async def match(artist, title):
        calls.append(title)
        return {"id": title}

    provider = PlaylistProvider(fetch, lambda row: None, match)
    snapshot = asyncio.run(provider.load("PL_test_playlist123"))
    assert len(calls) == 50
    assert (
        snapshot.entries[50]["reason"] == "Fallback search limit reached (50)"
    )
    assert snapshot.entries[51]["song_id"] == "0"


def test_metadata_outage_is_not_published_as_missing_matches():
    async def fetch(payload):
        return {
            "title": "Chart",
            "tracks": [
                SONG | {"videoType": "MUSIC_VIDEO_TYPE_OMV"},
            ],
        }

    async def match(artist, title):
        raise CatalogError()

    provider = PlaylistProvider(fetch, lambda row: None, match)
    with pytest.raises(CatalogError):
        asyncio.run(provider.load("PL_test_playlist123"))


def test_malformed_rows_are_reported_and_duration_and_artwork_are_normalized():
    captured = []

    async def fetch(payload):
        return {
            "title": "Chart",
            "tracks": [
                None,
                {"artists": "oops"},
                SONG
                | {
                    "duration_seconds": None,
                    "duration": "5:37",
                    "thumbnails": [
                        {"url": "http://127.0.0.1/private"},
                        {"url": "https://i.ytimg.com/vi/test/default.jpg"},
                    ],
                },
            ],
        }

    def song(row):
        captured.append(row)
        return {"id": "audio"}

    async def match(*args):
        raise AssertionError("Unexpected fallback")

    snapshot = asyncio.run(
        PlaylistProvider(fetch, song, match).load("PL_test_playlist123")
    )
    assert len(snapshot.entries) == 3
    assert snapshot.entries[0]["reason"] == "Unavailable"
    assert captured[0]["duration_seconds"] == 337
    assert captured[0]["thumbnails"] == [
        {"url": "https://i.ytimg.com/vi/test/default.jpg"},
    ]
