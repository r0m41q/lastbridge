import asyncio
import time
from types import SimpleNamespace

import httpx
from support.catalog import get
from support.mixes import pair, refresh, source, tracks
from support.sync import uid

import lastbridge.features.discovery.application.builder as mix_builder
from lastbridge.providers.lastfm.client import Lastfm
from lastbridge.shared.lastfm import LastfmRequestError


def test_sparse_similar_tracks_fall_back_to_artist_tracks(
    mix_service, monkeypatch
):
    service, calls = mix_service
    services = service[1].state.services
    original = services.lastfm.taste_rows

    async def taste(client, method, **params):
        if method == "track.getSimilar":
            return []
        if method == "artist.getTopTracks":
            return [pair(1), pair(3)]
        return await original(client, method, **params)

    monkeypatch.setattr(services.lastfm, "taste_rows", taste)
    refresh(service)
    assert [s["title"] for s in tracks(service, "Discover")] == ["Track 3"]
    assert not any(method == "chart.getTopTracks" for method, _ in calls)


def test_more_seeds_and_related_artists_supply_discovery(
    mix_service, monkeypatch
):
    service, _ = mix_service
    services = service[1].state.services
    calls = []

    async def taste(client, method, **params):
        calls.append((method, params))
        if method == "user.getTopTracks":
            return [pair(i) for i in range(1, 7)]
        if method == "track.getSimilar":
            assert params["autocorrect"] == "1"
            seeds = [p for m, p in calls if m == method]
            return [pair(7)] if len(seeds) > 3 else []
        if method == "artist.getSimilar":
            return [("Related Artist", "Related Artist")]
        if method == "artist.getTopTracks":
            return [pair(8)] if params["artist"] == "Related Artist" else []
        return []

    monkeypatch.setattr(services.lastfm, "taste_rows", taste)
    refresh(service)
    assert [s["title"] for s in tracks(service, "Discover")] == [
        "Track 7",
        "Track 8",
    ]
    assert len([m for m, _ in calls if m == "track.getSimilar"]) == 6
    assert len([m for m, _ in calls if m == "artist.getSimilar"]) <= 2
    assert len([m for m, _ in calls if m == "artist.getTopTracks"]) <= 3
    assert not any(m == "chart.getTopTracks" for m, _ in calls)


def test_discovery_gets_lookup_share_with_large_other_mixes(
    mix_service, monkeypatch
):
    service, _ = mix_service
    services = service[1].state.services
    lookups = []

    async def taste(client, method, **params):
        offset = {
            "user.getTopTracks": 0,
            "user.getRecentTracks": 100,
            "track.getSimilar": 200,
        }.get(method)
        return (
            [pair(offset + i) for i in range(1, 51)]
            if offset is not None
            else []
        )

    async def match(artist, title):
        lookups.append(title)
        return services.catalog.song(source(int(title.split()[-1])))

    monkeypatch.setattr(services.lastfm, "taste_rows", taste)
    monkeypatch.setattr(services.catalog, "match_loved", match)
    monkeypatch.setattr(mix_builder, "LOOKUPS", 8)
    refresh(service)
    assert len(lookups) == 8
    assert len(tracks(service, "Discover")) == 4
    assert len(tracks(service, "Comfort")) == 2
    assert len(tracks(service, "Listen Again")) == 2


def test_one_slow_match_does_not_discard_other_mixes(mix_service, monkeypatch):
    service, _ = mix_service
    services = service[1].state.services
    original = services.catalog.match_loved

    async def match(artist, title):
        if title == "Track 3":
            await asyncio.Event().wait()
        return await original(artist, title)

    monkeypatch.setattr(services.catalog, "match_loved", match)
    monkeypatch.setattr(mix_builder, "MATCH_TIMEOUT", 0.01)
    refresh(service)
    assert [s["title"] for s in tracks(service, "Discover")] == ["Track 4"]
    assert tracks(service, "Comfort") and tracks(service, "Listen Again")
    assert not services.mixes.status(uid(service))["error"]


def test_resolution_budget_keeps_partial_work_and_cached_songs(
    mix_service, monkeypatch
):
    service, _ = mix_service
    services = service[1].state.services
    saved = services.catalog.song(source(1))
    get(service, "star", id=saved["id"])
    clock, lookups = [0], []
    original = services.catalog.match_loved

    async def match(artist, title):
        lookups.append(title)
        result = await original(artist, title)
        clock[0] = 2
        return result

    monkeypatch.setattr(services.catalog, "match_loved", match)
    monkeypatch.setattr(mix_builder, "RESOLUTION_BUDGET", 1)
    monkeypatch.setattr(
        mix_builder,
        "time",
        SimpleNamespace(time=time.time, monotonic=lambda: clock[0]),
    )
    refresh(service)
    assert lookups == ["Track 3"]
    assert [s["title"] for s in tracks(service, "Discover")] == ["Track 3"]
    assert [s["title"] for s in tracks(service, "Comfort")] == ["Track 1"]
    assert not services.mixes.status(uid(service))["error"]


def test_fallback_does_not_reintroduce_known_tracks(mix_service, monkeypatch):
    service, _ = mix_service
    services = service[1].state.services
    original = services.lastfm.taste_rows

    async def taste(client, method, **params):
        if method == "track.getSimilar":
            return []
        if method == "artist.getTopTracks":
            return [pair(1), pair(2), pair(3)]
        return await original(client, method, **params)

    monkeypatch.setattr(services.lastfm, "taste_rows", taste)
    refresh(service)
    assert [s["title"] for s in tracks(service, "Discover")] == ["Track 3"]


def test_empty_and_failed_discovery_have_safe_diagnostics(
    mix_service, monkeypatch, caplog
):
    service, _ = mix_service
    services = service[1].state.services

    async def empty(client, method, **params):
        return []

    monkeypatch.setattr(services.lastfm, "taste_rows", empty)
    refresh(service)
    assert "reason=no_new_suggestions" in caplog.text

    async def fail(client, method, **params):
        raise LastfmRequestError("pending", "private session secret", 29)

    monkeypatch.setattr(services.lastfm, "taste_rows", fail)
    with services.database.transaction() as connection:
        connection.execute("UPDATE mixes SET next_attempt=0")
    refresh(service)
    assert "error_type=LastfmRequestError code=29" in caplog.text
    assert "private session secret" not in caplog.text


def test_related_artist_adapter_uses_public_api():
    async def scenario():
        def handler(request):
            assert request.url.params["method"] == "artist.getSimilar"
            assert request.url.params["artist"] == "Seed Artist"
            assert "sk" not in request.url.params
            return httpx.Response(
                200,
                json={"similarartists": {"artist": [{"name": "Related"}]}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            assert await Lastfm("key", "secret").taste_rows(
                client, "artist.getSimilar", artist="Seed Artist"
            ) == [("Related", "Related")]

    asyncio.run(scenario())
