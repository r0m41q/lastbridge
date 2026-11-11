import asyncio
import time

import pytest
from support.catalog import ARTIST, SONG, body, get, search

from lastbridge.shared.errors import CatalogError


def test_lastfm_order_deduplication_and_browsing_are_independent(tops_service):
    service, calls = tops_service
    catalog = service[1].state.services.catalog
    artist = search(service)["artist"][0]
    metadata = catalog.extras.metadata
    metadata.tracks = [
        (artist["name"], "Second"),
        (artist["name"], SONG["title"]),
        (artist["name"], "Second"),
    ]
    result = body(get(service, "getTopSongs", id=artist["id"]))["topSongs"]
    assert [s["title"] for s in result["song"]] == ["Second", SONG["title"]]
    get(service, "getArtist", id=artist["id"])
    assert body(get(service, "getTopSongs", id=artist["id"]))["topSongs"] == (
        result
    )
    assert len(calls) == 2 and len(metadata.calls) == 1
    assert calls[0]["query"] == f"{artist['name']} Second"


@pytest.mark.parametrize(
    "changes",
    [
        {"title": SONG["title"] + " (Live)"},
        {"title": SONG["title"] + " (Remix)"},
        {"isAvailable": False},
        {"videoType": "MUSIC_VIDEO_TYPE_OMV"},
        {"artists": [{"name": "Daft Punk & friends", "id": "UC_wrong"}]},
        {"album": {"id": "bad", "name": "Album"}},
    ],
)
def test_ranked_matching_rejects_wrong_recordings(catalog_service, changes):
    catalog = catalog_service[1].state.services.catalog
    artist = search(catalog_service)["artist"][0]
    assert (
        catalog.documents.ranked_song(
            catalog.read(artist["id"]),
            SONG["title"],
            {"songs": [SONG | changes]},
        )
        is None
    )


def test_equivalent_editions_choose_explicit_and_keep_one_catalog_record(
    catalog_service,
):
    catalog = catalog_service[1].state.services.catalog
    artist = search(catalog_service)["artist"][0]
    clean = SONG | {"isExplicit": False}
    explicit = SONG | {
        "isExplicit": True,
        "album": {"id": "MPRE_another", "name": "Another Album"},
    }
    selected = catalog.documents.ranked_song(
        catalog.read(artist["id"]),
        SONG["title"],
        {"songs": [clean, SONG, explicit, explicit]},
    )
    assert selected["isExplicit"] is True
    assert selected["album"] == "Another Album"
    assert catalog.read(selected["id"])["document"] == selected


def test_resolution_cache_survives_ranking_refresh(tops_service):
    service, calls = tops_service
    extras = service[1].state.services.catalog.extras
    artist = search(service)["artist"][0]
    original = body(get(service, "getTopSongs", id=artist["id"]))["topSongs"][
        "song"
    ]
    extras.tops.clear()
    extras.metadata.tracks.reverse()
    refreshed = body(get(service, "getTopSongs", id=artist["id"]))["topSongs"][
        "song"
    ]
    assert refreshed == original[::-1]
    assert len(calls) == 2 and len(extras.metadata.calls) == 2


def test_missing_matches_cache_briefly_and_can_recover(
    tops_service,
    monkeypatch,
):
    service, calls = tops_service
    catalog = service[1].state.services.catalog
    artist = search(service)["artist"][0]
    original = catalog.provider.fetch

    async def missing(payload):
        calls.append(payload)
        return {"songs": []}

    monkeypatch.setattr(catalog.provider, "fetch", missing)
    assert (
        body(get(service, "getTopSongs", id=artist["id"]))["topSongs"]["song"]
        == []
    )
    # Ranking freshness also shortens, so missing titles can recover when
    # the five-minute negative match cache expires.
    assert 0 < catalog.extras.tops[ARTIST][0] - time.monotonic() <= 300
    catalog.extras.tops.clear()
    monkeypatch.setattr(catalog.provider, "fetch", original)
    assert (
        body(get(service, "getTopSongs", id=artist["id"]))["topSongs"]["song"]
        == []
    )
    assert len(calls) == 2
    catalog.extras.tops.clear()
    for key, (_, song) in catalog.extras.matches.items():
        catalog.extras.matches[key] = (0, song)
    assert (
        len(
            body(get(service, "getTopSongs", id=artist["id"]))["topSongs"][
                "song"
            ]
        )
        == 2
    )
    assert len(calls) == 4


def test_resolution_timeout_cancels_workers_and_returns_ranked_partial(
    tops_service,
    monkeypatch,
):
    service, _ = tops_service
    catalog = service[1].state.services.catalog
    artist = search(service)["artist"][0]
    cancelled = []

    async def fetch(payload):
        if payload["query"].endswith("Second"):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        return {"songs": [SONG]}

    monkeypatch.setattr(catalog.provider, "fetch", fetch)
    monkeypatch.setattr(
        "lastbridge.features.catalog.application.artist_extras."
        "RESOLUTION_TIMEOUT",
        0.02,
    )
    songs = body(get(service, "getTopSongs", id=artist["id"]))["topSongs"][
        "song"
    ]
    assert [s["title"] for s in songs] == [SONG["title"]]
    assert cancelled == [True]
    assert catalog.extras.slots._value == 2


def test_resolution_fanout_and_ranked_page_are_bounded(
    tops_service,
    monkeypatch,
):
    service, _ = tops_service
    catalog = service[1].state.services.catalog
    artist = search(service)["artist"][0]
    catalog.extras.metadata.tracks = [
        (artist["name"], f"T{i}") for i in range(75)
    ]
    active, peak, calls = 0, 0, []

    async def fetch(payload):
        nonlocal active, peak
        calls.append(payload)
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.001)
            title = payload["query"].split()[-1]
            return {
                "songs": [
                    SONG
                    | {"title": title, "videoId": f"{int(title[1:]):011d}"}
                ]
            }
        finally:
            active -= 1

    monkeypatch.setattr(catalog.provider, "fetch", fetch)
    songs = body(get(service, "getTopSongs", id=artist["id"]))["topSongs"][
        "song"
    ]
    assert [s["title"] for s in songs] == [f"T{i}" for i in range(50)]
    assert len(calls) == 50 and peak == 2 and active == 0


def test_concurrent_rankings_share_work_and_cancellation_cleans_up(
    tops_service,
    monkeypatch,
):
    service, _ = tops_service
    catalog = service[1].state.services.catalog
    artist = search(service)["artist"][0]

    async def scenario():
        results = await asyncio.gather(
            catalog.top_songs(1, artist["id"]),
            catalog.top_songs(2, artist["id"]),
        )
        assert results[0] == results[1]
        assert len(catalog.extras.metadata.calls) == 1
        catalog.extras.tops.clear()
        catalog.extras.matches.clear()
        started, cleaned = asyncio.Event(), []

        async def blocked(payload):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)

        monkeypatch.setattr(catalog.provider, "fetch", blocked)
        task = asyncio.create_task(catalog.top_songs(1, artist["id"]))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned and catalog.extras.slots._value == 2
        assert not catalog.extras.tops

    service[0].portal.call(scenario)


def test_outage_suppression_and_unconfigured_ranking(tops_service):
    service, calls = tops_service
    extras = service[1].state.services.catalog.extras
    artist = search(service)["artist"][0]
    extras.metadata.fail = True
    for _ in range(2):
        response = body(get(service, "getTopSongs", id=artist["id"]))
        assert response["error"]["message"] == "Metadata provider unavailable"
    assert len(extras.metadata.calls) == 1 and not calls
    extras.metadata.configured = False
    assert (
        body(get(service, "getTopSongs", id=artist["id"]))["topSongs"]["song"]
        == []
    )


def test_artist_without_lastfm_top_tracks_returns_cached_empty_list(
    tops_service,
):
    service, calls = tops_service
    extras = service[1].state.services.catalog.extras
    artist = search(service)["artist"][0]
    extras.metadata.tracks = []
    for _ in range(2):
        response = body(get(service, "getTopSongs", id=artist["id"]))
        assert response["status"] == "ok"
        assert response["topSongs"]["song"] == []
    assert len(extras.metadata.calls) == 1 and not calls


def test_partial_failure_keeps_previous_ranked_snapshot(
    tops_service,
    monkeypatch,
):
    service, _ = tops_service
    catalog = service[1].state.services.catalog
    artist = search(service)["artist"][0]
    original = body(get(service, "getTopSongs", id=artist["id"]))["topSongs"]
    catalog.extras.tops[ARTIST] = (time.monotonic() - 1, original["song"])
    catalog.extras.matches.clear()

    async def fail_one(payload):
        if payload["query"].endswith("Second"):
            raise CatalogError()
        return {"songs": [SONG]}

    monkeypatch.setattr(catalog.provider, "fetch", fail_one)
    assert body(get(service, "getTopSongs", id=artist["id"]))["topSongs"] == (
        original
    )
