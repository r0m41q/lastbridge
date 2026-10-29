import asyncio
import time

import httpx
import pytest
import test_radio
from test_catalog import SONG, body, get, search
from test_radio import request, selected, track
from test_sync import rows, run, uid

import lastbridge.radio as radio_module
from lastbridge.catalog import CatalogError
from lastbridge.lastfm import Lastfm, LastfmRequestError

radio_service = test_radio.radio_service
sync_service = test_radio.sync_service
ORIGINAL_TASTE = Lastfm.taste_rows


@pytest.fixture
def fallback_service(radio_service, monkeypatch):
    service, _ = radio_service
    app = service[1]
    radio = app.state.radio
    radio.lastfm = app.state.lastfm
    radio.client = app.state.http_client
    calls = []
    original = app.state.catalog.provider.fetch

    async def fetch(payload):
        if payload["operation"] == "radio":
            return {"tracks": []}
        if payload["operation"] == "search" and payload["kinds"] == ["songs"]:
            return {"songs": [track(i) for i in range(1, 4)]}
        return await original(payload)

    async def taste(client, method, **parameters):
        calls.append((method, parameters))
        return [
            ("Daft Punk & friends", SONG["title"]),
            ("Daft Punk & friends", "Track 1"),
            ("Daft Punk & friends", "Track 1"),
            ("Daft Punk & friends", "Track 2"),
            ("Wrong Artist", "Track 3"),
        ]

    monkeypatch.setattr(app.state.catalog.provider, "fetch", fetch)
    monkeypatch.setattr(app.state.lastfm, "taste_rows", taste)
    return service, calls


@pytest.mark.parametrize("failure", ["empty", "error", "timeout", "filtered"])
def test_fallback_on_empty_failed_slow_or_exhausted_primary(
    fallback_service, monkeypatch, failure
):
    service, calls = fallback_service
    app = service[1]
    seed = search(service)["song"][0]

    async def primary(source):
        if failure == "error":
            raise CatalogError()
        if failure == "timeout":
            await asyncio.Event().wait()
        return [seed] if failure == "filtered" else []

    monkeypatch.setattr(app.state.catalog, "radio", primary)
    monkeypatch.setattr(radio_module, "PRIMARY_DEADLINE", 0.01)
    songs = selected(request(service))
    assert [s["title"] for s in songs] == ["Track 1", "Track 2"]
    assert calls == [
        (
            "track.getSimilar",
            {
                "artist": seed["artist"],
                "track": seed["title"],
                "autocorrect": "0",
            },
        )
    ]
    assert selected(request(service)) == songs
    assert len(calls) == 1
    assert (
        body(
            get(
                service,
                "getPlaylist",
                id=app.state.radio.playlist_id(uid(service)),
            )
        )["playlist"]["entry"]
        == songs
    )
    assert not rows(service, "favorites")
    assert not rows(service, "save_jobs")
    assert not app.state.audio.entries


def test_healthy_primary_does_not_call_lastfm(fallback_service, monkeypatch):
    service, calls = fallback_service
    app = service[1]
    song = app.state.catalog.song(track(9))

    async def primary(source):
        return [song]

    monkeypatch.setattr(app.state.catalog, "radio", primary)
    assert selected(request(service))[0]["id"] == song["id"]
    assert not calls


def test_shared_fallback_keeps_private_exclusions(fallback_service):
    service, calls = fallback_service
    songs = selected(request(service))
    get(service, "unstar", id=songs[0]["id"])
    get(service, "scrobble", id=songs[1]["id"], submission="false")
    assert body(request(service))["status"] == "failed"
    assert selected(request(service, apiKey=service[3]["alice"])) == songs
    assert len(calls) == 1


@pytest.mark.parametrize("configured", [True, False])
def test_unavailable_lastfm_retains_same_seed_snapshot(
    fallback_service, monkeypatch, configured
):
    service, _ = fallback_service
    songs = selected(request(service))
    app = service[1]
    app.state.radio.cache.clear()

    async def failed(*args, **kwargs):
        raise LastfmRequestError("pending", "sensitive upstream text")

    monkeypatch.setattr(app.state.lastfm, "taste_rows", failed)
    if not configured:
        app.state.radio.lastfm = None
    assert selected(request(service)) == songs
    other = app.state.catalog.song(track(9))
    assert (
        body(get(service, "getSimilarSongs", id=other["id"]))["error"]["code"]
        == 0
    )


def test_fallback_lookup_limit_and_partial_deadline(
    fallback_service, monkeypatch
):
    service, _ = fallback_service
    app = service[1]
    matches = []
    song = app.state.catalog.song(track(1))

    async def taste(*args, **kwargs):
        return [("Daft Punk & friends", f"Track {i}") for i in range(50)]

    async def match(artist, title):
        matches.append(title)
        if title == "Track 0":
            return song
        raise CatalogError(70, "Unavailable")

    monkeypatch.setattr(app.state.lastfm, "taste_rows", taste)
    monkeypatch.setattr(app.state.catalog, "match_loved", match)
    assert selected(request(service)) == [song]
    assert len(matches) == 10
    app.state.radio.cache.clear()
    matches.clear()

    async def slow_match(artist, title):
        matches.append(title)
        if len(matches) == 1:
            return song
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.catalog, "match_loved", slow_match)
    monkeypatch.setattr(radio_module, "FALLBACK_DEADLINE", 0.3)
    assert selected(request(service)) == [song]
    assert len(matches) == 2


def test_fallback_uses_public_api_without_session(fallback_service):
    service, _ = fallback_service
    app = service[1]
    seed = search(service)["song"][0]

    async def scenario():
        def handler(upstream_request):
            assert upstream_request.url.params["method"] == "track.getSimilar"
            assert upstream_request.url.params["artist"] == seed["artist"]
            assert upstream_request.url.params["track"] == seed["title"]
            assert "sk" not in upstream_request.url.params
            return httpx.Response(
                200,
                json={
                    "similartracks": {
                        "track": [
                            {
                                "artist": {"name": "Daft Punk & friends"},
                                "name": "Track 1",
                            }
                        ]
                    }
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            app.state.radio.lastfm = Lastfm("key", "secret")
            app.state.radio.lastfm.taste_rows = ORIGINAL_TASTE.__get__(
                app.state.radio.lastfm
            )
            app.state.radio.client = client
            result = await app.state.radio.songs(
                app.state.accounts.user("admin"), seed["id"], 50
            )
            assert [s["title"] for s in result] == ["Track 1"]

    run(service, scenario)


def test_empty_fallback_is_cooled_down(fallback_service, monkeypatch):
    service, _ = fallback_service
    app = service[1]
    calls = []

    async def empty(*args, **kwargs):
        calls.append(1)
        return []

    monkeypatch.setattr(app.state.lastfm, "taste_rows", empty)
    assert body(request(service))["status"] == "failed"
    assert body(request(service))["status"] == "failed"
    assert len(calls) == 1
    source = SONG["videoId"]
    app.state.radio.cache[("lastfm", source)] = (time.monotonic() - 61, [])
    assert body(request(service))["status"] == "failed"
    assert len(calls) == 2


def test_fallback_waiters_share_work_and_survive_disconnect(
    fallback_service, monkeypatch
):
    service, calls = fallback_service
    app = service[1]
    seed = search(service)["song"][0]
    original = app.state.lastfm.taste_rows

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(app.state.lastfm, "taste_rows", slow)
        first = asyncio.create_task(
            app.state.radio.songs(
                app.state.accounts.user("admin"), seed["id"], 50
            )
        )
        await entered.wait()
        second = asyncio.create_task(
            app.state.radio.songs(
                app.state.accounts.user("alice"), seed["id"], 50
            )
        )
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        release.set()
        assert len(await second) == 2
        assert len(calls) == 1
        assert not app.state.radio.generations

    run(service, scenario)


def test_zero_and_head_do_not_start_fallback(fallback_service):
    service, calls = fallback_service
    seed = search(service)["song"][0]
    assert selected(request(service, count=0)) == []
    response = service[0].head(
        "/rest/getSimilarSongs",
        params={
            "id": seed["id"],
            "apiKey": service[3]["admin"],
            "v": "1.16.1",
            "c": "test",
            "f": "json",
        },
    )
    assert response.status_code == 200
    assert not calls
    assert not service[1].state.radio.tasks
