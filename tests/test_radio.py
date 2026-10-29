import asyncio
import copy
import json
import time
from xml.etree.ElementTree import fromstring

import httpx
import pytest
import test_sync
from test_accounts import csrf, drop_playlist_tables, login
from test_catalog import SONG, body, get, search
from test_sync import rows, run, uid

import lastbridge.radio as radio_module
from lastbridge.accounts import Accounts
from lastbridge.catalog import CatalogError
from lastbridge.providers.catalog_worker import execute
from lastbridge.radio import Radio

sync_service = test_sync.sync_service


def track(index, **changes):
    return (
        copy.deepcopy(SONG)
        | {
            "videoId": f"{index:011d}",
            "title": f"Track {index}",
            "length": "3:04",
            "duration_seconds": None,
        }
        | changes
    )


@pytest.fixture
def radio_service(sync_service, monkeypatch):
    service = sync_service
    app = service[1]
    app.state.radio = Radio(
        app.state.accounts, app.state.catalog, app.state.library
    )
    calls = []
    original = app.state.catalog.provider.fetch

    async def fetch(payload):
        if payload["operation"] != "radio":
            return await original(payload)
        calls.append(payload)
        return {
            "tracks": [
                SONG,
                track(1),
                track(1),
                track(2),
                track(3),
                track(4, isAvailable=False),
                track(5, videoType="MUSIC_VIDEO_TYPE_OMV"),
            ]
        }

    monkeypatch.setattr(app.state.catalog.provider, "fetch", fetch)
    search(service)
    return service, calls


def request(service, **params):
    seed = search(service)["song"][0]
    return get(service, "getSimilarSongs", id=seed["id"], **params)


def selected(response, field="similarSongs"):
    return body(response)[field]["song"]


def test_song_radio_filters_duplicates_unavailable_and_seed(radio_service):
    service, calls = radio_service
    songs = selected(request(service))
    assert [s["title"] for s in songs] == ["Track 1", "Track 2", "Track 3"]
    assert all(s["duration"] == 184 for s in songs)
    assert all(s["contentType"] == "application/octet-stream" for s in songs)
    assert len(calls) == 1
    assert selected(request(service, count=1)) == songs[:1]
    assert len(calls) == 1
    assert not rows(service, "favorites") and not rows(service, "save_jobs")
    assert not rows(service, "scrobbles")


def test_recent_starts_reports_and_removals_are_private(radio_service):
    service, calls = radio_service
    songs = selected(request(service))
    get(service, "scrobble", id=songs[0]["id"], submission="false")
    get(
        service,
        "scrobble",
        id=songs[1]["id"],
        time=int((time.time() - 180) * 1000),
    )
    get(service, "unstar", id=songs[2]["id"])
    assert body(request(service))["status"] == "failed"
    other = selected(request(service, apiKey=service[3]["alice"]))
    assert len(other) == 3
    assert sum(c["source"] == SONG["videoId"] for c in calls) == 1
    assert len(rows(service, "scrobbles")) == 1


def test_album_artist_form_xml_and_count_errors(radio_service):
    service, _ = radio_service
    result = search(service)
    for kind in ("album", "artist"):
        response = get(service, "getSimilarSongs2", id=result[kind][0]["id"])
        assert selected(response, "similarSongs2")
    response = service[0].post(
        "/rest/getSimilarSongs.view",
        data={
            "apiKey": service[3]["admin"],
            "v": "1.16.1",
            "c": "Feishin",
            "id": result["song"][0]["id"],
            "count": "2",
        },
    )
    assert (
        len(
            fromstring(response.content)
            .find("{*}similarSongs")
            .findall("{*}song")
        )
        == 2
    )
    for count in ("-1", "bad", str(2**31)):
        assert body(request(service, count=count))["error"]["code"] == 10
    assert selected(request(service, count=0)) == []
    assert (
        body(get(service, "getSimilarSongs", id="unknown"))["error"]["code"]
        == 70
    )
    assert body(get(service, "getSimilarSongs"))["error"]["code"] == 10
    assert (
        body(service[0].get("/rest/getSimilarSongs?f=json"))["error"]["code"]
        == 40
    )


def test_private_playlist_restart_and_same_seed_outage_fallback(
    radio_service, monkeypatch
):
    service, _ = radio_service
    app = service[1]
    songs = selected(request(service))
    user = app.state.accounts.user("admin")
    playlist = app.state.radio.playlist(user)
    assert playlist["songCount"] == 3 and playlist["public"] is False
    response = get(service, "getPlaylist", id=playlist["id"])
    assert body(response)["playlist"]["entry"] == songs
    assert (
        len(body(get(service, "getPlaylists"))["playlists"]["playlist"]) == 5
    )
    assert (
        body(
            get(
                service,
                "getPlaylist",
                id=playlist["id"],
                apiKey=service[3]["alice"],
            )
        )["error"]["code"]
        == 70
    )

    async def unavailable(payload):
        raise CatalogError()

    monkeypatch.setattr(app.state.catalog.provider, "fetch", unavailable)
    restored = Radio(
        Accounts(app.state.accounts.path.parent),
        app.state.catalog,
        app.state.library,
    )
    app.state.radio = restored
    seed = rows(service, "radio")[0]["seed_id"]
    assert run(service, restored.songs, user, seed, 50) == songs
    different = app.state.catalog.song(track(9))
    with pytest.raises(CatalogError, match="Radio unavailable"):
        run(service, restored.songs, user, different["id"], 50)


def test_shared_fetch_waiter_cancellation_and_shutdown(
    radio_service, monkeypatch
):
    service, _ = radio_service
    app = service[1]
    original = app.state.catalog.radio
    calls = []
    seed = search(service)["song"][0]["id"]

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(source):
            calls.append(source)
            entered.set()
            await release.wait()
            return await original(source)

        monkeypatch.setattr(app.state.catalog, "radio", slow)
        first = asyncio.create_task(
            app.state.radio.songs(app.state.accounts.user("admin"), seed, 50)
        )
        await entered.wait()
        second = asyncio.create_task(
            app.state.radio.songs(app.state.accounts.user("alice"), seed, 50)
        )
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        release.set()
        assert len(await second) == 3 and len(calls) == 1
        assert not app.state.radio.generations
        await app.state.radio.close()
        assert not app.state.radio.tasks

    run(service, scenario)


def test_metadata_warm_is_bounded_and_does_not_select_radio(radio_service):
    service, calls = radio_service
    app = service[1]
    song = search(service)["song"][0]

    async def scenario():
        for _ in range(20):
            app.state.radio.warm(uid(service), song["id"])
        pending = list(app.state.radio.tasks.values())
        assert len(pending) == 1
        await asyncio.gather(*pending)
        assert (
            app.state.radio.playlist(app.state.accounts.user("admin")) is None
        )

    run(service, scenario)
    assert len(calls) == 1 and not rows(service, "radio")
    assert selected(request(service)) and len(calls) == 1


def test_browser_radio_csrf_and_private_choices(radio_service):
    service, _ = radio_service
    client = service[0]
    song = search(service)["song"][0]
    get(service, "star", id=song["id"])
    assert client.get("/radio", follow_redirects=False).status_code == 303
    login(client)
    view = client.get("/radio")
    assert "Instant Crush &lt;studio&gt;" in view.text
    assert (
        client.post(
            "/radio", data={"csrf": "bad", "id": song["id"]}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/radio",
            data={"csrf": csrf(view), "id": song["id"]},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert "Radio ·" in client.get("/radio").text


def test_relinked_identity_hides_and_cannot_publish_old_radio(
    radio_service, monkeypatch
):
    service, _ = radio_service
    app = service[1]
    request(service)
    original = app.state.catalog.radio
    app.state.radio.cache.clear()

    async def switched(source):
        with app.state.accounts.transaction() as connection:
            connection.execute(
                "UPDATE users SET lastfm_name='different' WHERE id=?",
                (uid(service),),
            )
        return await original(source)

    monkeypatch.setattr(app.state.catalog, "radio", switched)
    request(service)
    user = app.state.accounts.user("admin")
    assert app.state.radio.playlist(user) is None
    assert rows(service, "radio")[0]["target"] == "admin-fm"


def test_v5_upgrade_preserves_mixes_and_sync(radio_service):
    service, _ = radio_service
    app = service[1]
    app.state.mixes.linked(uid(service))
    before = rows(service, "mixes")
    with app.state.accounts.transaction() as connection:
        drop_playlist_tables(connection)
        connection.execute("DROP TABLE lyrics")
        connection.execute("DROP TABLE radio")
        connection.execute("PRAGMA user_version=5")
    store = Accounts(app.state.accounts.path.parent)
    assert rows(service, "mixes") == before
    assert store.client_user({"apiKey": service[3]["admin"]})
    with store.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9


@pytest.mark.parametrize(
    "changes",
    [
        {"videoId": []},
        {"album": None},
        {"artists": [None]},
        {"title": None},
        {"length": "3:99"},
        {"length": "live"},
        {"length": "0:10"},
        {"isAvailable": False},
        {"videoType": "MUSIC_VIDEO_TYPE_OMV"},
    ],
)
def test_malformed_watch_rows_are_omitted(radio_service, monkeypatch, changes):
    service, _ = radio_service
    app = service[1]

    async def fetch(payload):
        return {"tracks": [track(1, **changes), track(2)]}

    monkeypatch.setattr(app.state.catalog.provider, "fetch", fetch)
    assert [s["title"] for s in selected(request(service))] == ["Track 2"]


def test_worker_radio_call_and_metadata_allowlist(monkeypatch):
    class Music:
        def get_watch_playlist(self, **kwargs):
            assert kwargs == {
                "videoId": SONG["videoId"],
                "radio": True,
                "limit": 50,
            }
            return {
                "tracks": [
                    track(1)
                    | {
                        "feedbackTokens": "secret",
                        "counterpart": {"url": "secret"},
                    }
                ]
            }

    monkeypatch.setattr("ytmusicapi.YTMusic", Music)
    result = execute({"operation": "radio", "source": SONG["videoId"]})
    assert "secret" not in json.dumps(result)
    assert result["tracks"][0]["videoType"] == "MUSIC_VIDEO_TYPE_ATV"


def test_video_radio_reseeds_with_exact_official_audio(monkeypatch):
    calls = []
    audio_seed = track(42)
    video_seed = SONG | {"album": None, "videoType": "MUSIC_VIDEO_TYPE_OMV"}

    class Music:
        def get_watch_playlist(self, **kwargs):
            calls.append(kwargs["videoId"])
            return {"tracks": [video_seed] if len(calls) == 1 else [track(1)]}

        def search(self, query, **kwargs):
            assert kwargs == {"filter": "songs", "limit": 20}
            assert SONG["title"] in query
            return [
                audio_seed | {"title": SONG["title"] + " (Live)"},
                audio_seed
                | {
                    "title": SONG["title"],
                    "artists": [{"name": "Wrong Artist"}],
                },
                audio_seed | {"title": SONG["title"]},
            ]

    monkeypatch.setattr("ytmusicapi.YTMusic", Music)
    result = execute({"operation": "radio", "source": SONG["videoId"]})
    assert calls == [SONG["videoId"], audio_seed["videoId"]]
    assert result["tracks"][0]["videoId"] == track(1)["videoId"]


def test_video_radio_does_not_use_a_different_version(monkeypatch):
    calls = []
    video_seed = SONG | {"album": None, "videoType": "MUSIC_VIDEO_TYPE_OMV"}

    class Music:
        def get_watch_playlist(self, **kwargs):
            calls.append(kwargs["videoId"])
            return {"tracks": [video_seed]}

        def search(self, query, **kwargs):
            return [track(42, title=SONG["title"] + " (Remix)")]

    monkeypatch.setattr("ytmusicapi.YTMusic", Music)
    result = execute({"operation": "radio", "source": SONG["videoId"]})
    assert len(calls) == 1
    assert result["tracks"][0]["videoType"] == "MUSIC_VIDEO_TYPE_OMV"


def test_listening_session_seek_save_replay_and_scrobble(radio_service):
    service, _ = radio_service
    client, app = service[:2]
    seed = search(service)["song"][0]
    assert get(service, "stream", id=seed["id"]).status_code == 200
    songs = selected(request(service))
    next_song = songs[0]
    params = {
        "apiKey": service[3]["admin"],
        "c": "Feishin",
        "v": "1.16.1",
        "id": next_song["id"],
    }
    response = client.get(
        "/rest/stream", params=params, headers={"Range": "bytes=10-19"}
    )
    assert response.status_code == 206 and len(response.content) == 10

    async def lyrics():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "artistName": next_song["artist"],
                        "trackName": next_song["title"],
                        "albumName": next_song["album"],
                        "duration": next_song["duration"],
                        "syncedLyrics": "[00:01.25] Synthetic session line",
                    },
                )
            )
        ) as upstream:
            app.state.lyrics.client = upstream
            result = await app.state.lyrics.get(next_song["id"])
            assert result[0]["synced"] is True
            assert result[0]["line"][0]["start"] == 1250

    run(service, lyrics)
    get(service, "star", id=next_song["id"])
    assert run(service, app.state.library.process_one)
    calls = list(app.state.audio.provider.calls)
    assert get(service, "stream", id=next_song["id"]).status_code == 200
    assert app.state.audio.provider.calls == calls
    assert app.state.library.downloads(uid(service))[0]["state"] == "complete"
    stamp = int((time.time() - 180) * 1000)
    for _ in range(2):
        assert (
            body(get(service, "scrobble", id=next_song["id"], time=stamp))[
                "status"
            ]
            == "ok"
        )
    assert len(rows(service, "scrobbles")) == 1

    async def deliver():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=test_sync.accepted())
            )
        ) as upstream:
            app.state.sync.client = upstream
            assert await app.state.sync.deliver_one()

    run(service, deliver)
    assert rows(service, "scrobbles")[0]["state"] == "sent"
    assert next_song["id"] not in {s["id"] for s in selected(request(service))}
    assert not app.state.library.stars(uid(service, "alice"))


def test_radio_deadline_is_bounded_and_errors_are_safe(
    radio_service, monkeypatch
):
    service, _ = radio_service

    async def blocked(source):
        await asyncio.Event().wait()

    monkeypatch.setattr(service[1].state.catalog, "radio", blocked)
    monkeypatch.setattr(radio_module, "DEADLINE", 0.01)
    response = request(service)
    assert body(response)["error"]["code"] == 0
    assert not rows(service, "radio")


def test_global_task_and_cache_limits(radio_service, monkeypatch):
    service, _ = radio_service
    app = service[1]

    async def scenario():
        gate = asyncio.Event()

        async def blocked(source):
            await gate.wait()
            return []

        monkeypatch.setattr(app.state.catalog, "radio", blocked)
        for i in range(4):
            app.state.radio.task(f"{i:011d}")
        with pytest.raises(CatalogError, match="busy"):
            app.state.radio.task("00000000099")
        gate.set()
        await asyncio.gather(*list(app.state.radio.tasks.values()))
        for i in range(40):
            await app.state.radio.fetch(f"{i:011d}")
        assert len(app.state.radio.cache) == 32

    run(service, scenario)
