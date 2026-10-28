import asyncio
import copy
import time
from xml.etree.ElementTree import fromstring

import httpx
import pytest
import test_sync
from test_accounts import csrf, login
from test_catalog import SONG, body, get
from test_sync import rows, run, uid

import lastbridge.mixes as mix_module
from lastbridge.accounts import Accounts
from lastbridge.catalog import CatalogError
from lastbridge.lastfm import Lastfm, LastfmRequestError
from lastbridge.mixes import Mixes
from lastbridge.recordings import recording_key

sync_service = test_sync.sync_service


def source(index, title=None):
    return copy.deepcopy(SONG) | {
        "videoId": f"{index:011d}",
        "title": title or f"Track {index}",
        "artists": [{"name": "Test Artist", "id": "UC_test_artist"}],
    }


def pair(index):
    return "Test Artist", f"Track {index}"


@pytest.fixture
def mix_service(sync_service, monkeypatch):
    service = sync_service
    app = service[1]
    for name in ("admin", "alice"):
        app.state.mixes.linked(uid(service, name))
    calls = []

    async def taste(client, method, **params):
        calls.append((method, params))
        return {
            "user.getTopTracks": [pair(1)],
            "user.getTopArtists": [("Test Artist", "Test Artist")],
            "user.getRecentTracks": [pair(2), pair(2)],
            "track.getSimilar": [pair(1), pair(3), pair(3), pair(4)],
        }.get(method, [])

    async def fetch(payload):
        query = payload["query"]
        return {
            "songs": [
                source(i) for i in range(1, 9) if query == " ".join(pair(i))
            ]
        }

    monkeypatch.setattr(app.state.lastfm, "taste_rows", taste)
    monkeypatch.setattr(app.state.catalog.provider, "fetch", fetch)
    return service, calls


def refresh(service):
    assert run(service, service[1].state.mixes.refresh_one)


def playlists(service, **params):
    return body(get(service, "getPlaylists", **params))["playlists"][
        "playlist"
    ]


def tracks(service, name, **params):
    item = next(p for p in playlists(service, **params) if p["name"] == name)
    return body(get(service, "getPlaylist", id=item["id"], **params))[
        "playlist"
    ]["entry"]


def test_private_stable_playlists_policies_and_no_audio_saves(mix_service):
    service, calls = mix_service
    before = playlists(service)
    assert [p["name"] for p in before] == [
        "Comfort",
        "Discover",
        "Listen Again",
        "Loved Tracks",
    ]
    assert all(p["songCount"] == 0 and p["public"] is False for p in before)
    refresh(service)
    assert [s["title"] for s in tracks(service, "Comfort")] == ["Track 1"]
    assert [s["title"] for s in tracks(service, "Listen Again")] == ["Track 2"]
    assert [s["title"] for s in tracks(service, "Discover")] == [
        "Track 3",
        "Track 4",
    ]
    assert [p["id"] for p in before] == [p["id"] for p in playlists(service)]
    assert not rows(service, "save_jobs") and not rows(service, "favorites")
    alice = playlists(service, apiKey=service[3]["alice"])
    assert all(p["songCount"] == 0 for p in alice)
    assert not set(p["id"] for p in before) & set(p["id"] for p in alice)
    assert (
        body(
            get(
                service,
                "getPlaylist",
                id=before[0]["id"],
                apiKey=service[3]["alice"],
            )
        )["error"]["code"]
        == 70
    )
    assert (
        body(get(service, "getPlaylists", username="alice"))["error"]["code"]
        == 50
    )
    assert all("sk" not in params for _, params in calls)
    assert (
        run(service, service[1].state.mixes.refresh_one) is not False
    )  # Alice is still due.


def test_json_form_xml_metadata_and_read_only(mix_service):
    service, _ = mix_service
    refresh(service)
    item = playlists(service)[1]
    client = service[0]
    response = client.post(
        "/rest/getPlaylist.view",
        data={
            "apiKey": service[3]["admin"],
            "v": "1.16.1",
            "c": "test",
            "id": item["id"],
        },
    )
    root = fromstring(response.content)
    playlist = root.find("{*}playlist")
    assert playlist.attrib["public"] == "false"
    entries = playlist.findall("{*}entry")
    assert len(entries) == 2
    assert int(playlist.attrib["duration"]) == sum(
        int(s.attrib["duration"]) for s in entries
    )
    assert all(
        s.attrib["contentType"] == "application/octet-stream" for s in entries
    )
    for method in ("createPlaylist", "updatePlaylist", "deletePlaylist"):
        assert (
            body(get(service, method, playlistId=item["id"]))["error"]["code"]
            == 50
        )
    assert body(get(service, "getPlaylist"))["error"]["code"] == 10


def test_outage_keeps_snapshot_and_private_profile_after_restart(
    mix_service, monkeypatch
):
    service, _ = mix_service
    refresh(service)
    app = service[1]
    initial = tracks(service, "Discover")
    saved = app.state.mixes.status(uid(service))

    async def fail(*args, **kwargs):
        raise LastfmRequestError("pending", "secret provider body")

    monkeypatch.setattr(app.state.lastfm, "taste_rows", fail)
    with app.state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE mixes SET next_attempt=0 WHERE user_id=?", (uid(service),)
        )
    refresh(service)
    status = app.state.mixes.status(uid(service))
    assert (
        status["snapshot"] == saved["snapshot"]
        and status["profile"] == saved["profile"]
    )
    assert status["next_attempt"] > time.time() + 290
    assert "secret" not in status["error"]
    assert tracks(service, "Discover") == initial
    restarted = Accounts(app.state.accounts.path.parent)
    mixes = Mixes(
        restarted,
        app.state.catalog,
        app.state.library,
        app.state.lastfm,
        app.state.http_client,
    )
    assert mixes.playlists(restarted.user("admin")) == playlists(service)


def test_empty_profile_chart_fallback_never_fakes_history(
    mix_service, monkeypatch
):
    service, _ = mix_service

    async def empty(client, method, **params):
        return [pair(3)] if method == "chart.getTopTracks" else []

    monkeypatch.setattr(service[1].state.lastfm, "taste_rows", empty)
    refresh(service)
    assert [s["title"] for s in tracks(service, "Discover")] == ["Track 3"]
    assert tracks(service, "Comfort") == tracks(service, "Listen Again") == []


def test_top_artists_without_tracks_supply_discovery_seeds(
    mix_service, monkeypatch
):
    service, _ = mix_service

    async def empty(client, method, **params):
        if method == "user.getTopArtists":
            return [("Test Artist", "Test Artist")]
        if method == "artist.getTopTracks":
            return [pair(3)]
        return []

    monkeypatch.setattr(service[1].state.lastfm, "taste_rows", empty)
    refresh(service)
    assert [s["title"] for s in tracks(service, "Discover")] == ["Track 3"]


def test_live_loves_local_removals_and_newly_heard_discovery(mix_service):
    service, _ = mix_service
    app = service[1]
    song = app.state.catalog.song(source(3))
    get(service, "star", id=song["id"])
    with app.state.accounts.transaction() as connection:
        connection.execute(
            "INSERT INTO "
            "loved_items(user_id,target,recording_key,artist,title,state,"
            "song_id) VALUES(?,?,?,?,?,'matched',?)",
            (
                uid(service),
                "admin-fm",
                recording_key(*pair(3)),
                *pair(3),
                song["id"],
            ),
        )
    refresh(service)
    assert [s["title"] for s in tracks(service, "Loved Tracks")] == ["Track 3"]
    assert "Track 3" not in [s["title"] for s in tracks(service, "Discover")]
    get(service, "unstar", id=song["id"])
    assert tracks(service, "Loved Tracks") == []
    get(service, "star", id=song["id"])
    assert [s["title"] for s in tracks(service, "Loved Tracks")] == ["Track 3"]
    get(service, "unstar", id=song["id"])
    assert tracks(service, "Loved Tracks") == []
    another = app.state.catalog.song(source(4))
    get(
        service,
        "scrobble",
        id=another["id"],
        time=int((time.time() - 180) * 1000),
    )
    assert "Track 4" not in [s["title"] for s in tracks(service, "Discover")]


def test_browser_refresh_csrf_private_status_and_cooldown(mix_service):
    service, _ = mix_service
    client, app = service[:2]
    assert client.get("/mixes", follow_redirects=False).status_code == 303
    login(client)
    view = client.get("/mixes")
    assert view.status_code == 200 and "Comfort" in view.text
    assert "admin-session" not in view.text and "alice-fm" not in view.text
    assert (
        client.post("/mixes/refresh", data={"csrf": "bad"}).status_code == 403
    )
    assert (
        client.post(
            "/mixes/refresh",
            data={"csrf": csrf(view)},
            headers={"origin": "https://evil.test"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/mixes/refresh", data={"csrf": csrf(view)}, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        client.post("/mixes/refresh", data={"csrf": csrf(view)}).status_code
        == 409
    )
    assert app.state.mixes.status(uid(service, "alice"))["requested"] == 0


def test_changed_link_discards_inflight_results_and_old_target_content(
    mix_service, monkeypatch
):
    service, _ = mix_service
    app = service[1]
    refresh(service)
    old = playlists(service)
    original = app.state.lastfm.taste_rows
    changed = False

    async def change(client, method, **params):
        nonlocal changed
        if not changed:
            changed = True
            with app.state.accounts.transaction() as connection:
                connection.execute(
                    "UPDATE users SET lastfm_name='new-name' WHERE id=?",
                    (uid(service),),
                )
            assert app.state.mixes.status(uid(service)) is None
            app.state.mixes.linked(uid(service))
        return await original(client, method, **params)

    monkeypatch.setattr(app.state.lastfm, "taste_rows", change)
    with app.state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE mixes SET next_attempt=0 WHERE user_id=?", (uid(service),)
        )
    refresh(service)
    status = app.state.mixes.status(uid(service))
    assert status["target"] == "new-name" and status["snapshot"] == "{}"
    assert not status["running"]
    assert all(p["songCount"] == 0 for p in playlists(service))
    assert [p["id"] for p in old] == [p["id"] for p in playlists(service)]


def test_unavailable_ambiguous_duplicate_versions_filtered(
    mix_service, monkeypatch
):
    service, _ = mix_service
    app = service[1]
    original = app.state.catalog.provider.fetch

    async def fetch(payload):
        rows = await original(payload)
        if payload["query"] == " ".join(pair(3)):
            rows["songs"] += [
                source(5, "Track 3 (Live)"),
                source(6, "Track 3"),
            ]
        if payload["query"] == " ".join(pair(4)):
            rows["songs"][0]["isAvailable"] = False
        return rows

    monkeypatch.setattr(app.state.catalog.provider, "fetch", fetch)
    refresh(service)
    assert tracks(service, "Discover") == []
    assert len(tracks(service, "Comfort")) == 1


def test_v4_upgrade_preserves_sync_and_saved_state(mix_service):
    service, _ = mix_service
    app = service[1]
    song = app.state.catalog.song(source(1))
    get(service, "star", id=song["id"])
    get(
        service,
        "scrobble",
        id=song["id"],
        time=int((time.time() - 180) * 1000),
    )
    before = {
        name: rows(service, name)
        for name in ("favorites", "save_jobs", "scrobbles", "love_imports")
    }
    with app.state.accounts.transaction() as connection:
        connection.execute("DROP TABLE mixes")
        connection.execute("PRAGMA user_version=4")
    migrated = Accounts(app.state.accounts.path.parent)
    for name, expected in before.items():
        assert rows(service, name) == expected
    assert (
        migrated.client_user({"apiKey": service[3]["admin"]})["username"]
        == "admin"
    )
    with migrated.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute("SELECT count(*) FROM mixes").fetchone()[0] == 0
        )


@pytest.mark.parametrize(
    ("method", "root", "credit"),
    [
        ("user.getTopTracks", "toptracks", {"name": "OTOY"}),
        ("user.getRecentTracks", "recenttracks", {"#text": "OTOY"}),
        ("track.getSimilar", "similartracks", {"name": "OTOY"}),
        ("chart.getTopTracks", "tracks", {"name": "OTOY"}),
        ("artist.getTopTracks", "toptracks", {"name": "OTOY"}),
    ],
)
def test_taste_adapter_json_shapes_and_no_session(method, root, credit):
    async def scenario():
        def handler(request):
            assert (
                "sk" not in request.url.params
                and "api_sig" not in request.url.params
            )
            assert request.url.params["limit"] == "50"
            assert request.url.params["method"] == method
            return httpx.Response(
                200,
                json={root: {"track": {"artist": credit, "name": "Судоми"}}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            assert await Lastfm("key", "secret").taste_rows(
                client, method, user="test"
            ) == [("OTOY", "Судоми")]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "result",
    [
        [],
        {"track": "bad"},
        {"track": [{"name": "x", "artist": None}]},
        {"track": [{}]},
        {"track": [{}] * 52},
    ],
)
def test_malformed_taste_data_is_safe_failure(result):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"toptracks": result})
            )
        ) as client:
            with pytest.raises(
                LastfmRequestError, match="Invalid Last.fm taste"
            ):
                await Lastfm("key", "secret").taste_rows(
                    client, "user.getTopTracks", user="test"
                )

    asyncio.run(scenario())


def test_recent_now_playing_is_excluded_and_top_artist_parsed():
    async def scenario():
        def handler(request):
            if request.url.params["method"] == "user.getTopArtists":
                return httpx.Response(
                    200, json={"topartists": {"artist": [{"name": "OTOY"}]}}
                )
            return httpx.Response(
                200,
                json={
                    "recenttracks": {
                        "track": [
                            {
                                "name": "Playing",
                                "artist": {"#text": "Artist"},
                                "@attr": {"nowplaying": "true"},
                            },
                            {"name": "Судоми", "artist": {"#text": "OTOY"}},
                        ]
                    }
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            lastfm = Lastfm("key", "secret")
            assert await lastfm.taste_rows(
                client, "user.getRecentTracks", user="test"
            ) == [("OTOY", "Судоми")]
            assert await lastfm.taste_rows(
                client, "user.getTopArtists", user="test"
            ) == [("OTOY", "OTOY")]

    asyncio.run(scenario())


def test_refresh_lookup_budget_track_caps_and_reuse(mix_service, monkeypatch):
    service, _ = mix_service
    app = service[1]
    lookups = []

    async def taste(client, method, **params):
        return (
            [pair(i) for i in range(1, 51)]
            if method in ("user.getTopTracks", "track.getSimilar")
            else []
        )

    async def match(artist, title):
        lookups.append(title)
        return app.state.catalog.song(source(int(title.split()[-1])))

    monkeypatch.setattr(app.state.lastfm, "taste_rows", taste)
    monkeypatch.setattr(app.state.catalog, "match_loved", match)
    # Lower the budget to prove a hard bound without slowing the test.
    monkeypatch.setattr(mix_module, "LOOKUPS", 4)
    refresh(service)
    assert len(lookups) == len(tracks(service, "Comfort")) == 4
    with app.state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE mixes SET next_attempt=0 WHERE user_id=?", (uid(service),)
        )
    refresh(service)
    assert len(lookups) == 8 and len(tracks(service, "Comfort")) == 8
    # Existing local metadata can fill a mix without provider resolution.
    for i in range(1, 51):
        song = app.state.catalog.song(source(i))
        with app.state.accounts.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO favorites VALUES(?,?,?)",
                (uid(service), song["id"], "2026-10-29T00:00:00Z"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO favorite_tracks VALUES(?,?,?)",
                (uid(service), song["id"], song["id"]),
            )
    with app.state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE mixes SET next_attempt=0 WHERE user_id=?", (uid(service),)
        )
    refresh(service)
    assert len(lookups) == 8 and len(tracks(service, "Comfort")) == 30


def test_missing_seed_and_unavailable_recording_do_not_erase_other_results(
    mix_service, monkeypatch
):
    service, _ = mix_service
    app = service[1]
    original = app.state.lastfm.taste_rows

    async def taste(client, method, **params):
        if method == "track.getSimilar" and params["track"] == "Track 1":
            raise LastfmRequestError("failed", "Not found", 6)
        return await original(client, method, **params)

    original_match = app.state.catalog.match_loved

    async def match(artist, title):
        if title == "Track 3":
            raise CatalogError(70, "Unavailable")
        return await original_match(artist, title)

    monkeypatch.setattr(app.state.lastfm, "taste_rows", taste)
    monkeypatch.setattr(app.state.catalog, "match_loved", match)
    refresh(service)
    assert [s["title"] for s in tracks(service, "Discover")] == ["Track 4"]
    assert len(tracks(service, "Comfort")) == 1


def test_deadline_preserves_snapshot_and_releases_claim(
    mix_service, monkeypatch
):
    service, _ = mix_service
    app = service[1]
    refresh(service)
    old = app.state.mixes.status(uid(service))["snapshot"]

    async def stalled(client, method, **params):
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.lastfm, "taste_rows", stalled)
    monkeypatch.setattr(mix_module, "DEADLINE", 0.01)
    with app.state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE mixes SET next_attempt=0 WHERE user_id=?", (uid(service),)
        )
    refresh(service)
    status = app.state.mixes.status(uid(service))
    assert not status["running"] and status["snapshot"] == old
    assert status["error"] and status["next_attempt"] > time.time()


def test_interrupted_refresh_recovers_on_restart_without_losing_snapshot(
    mix_service,
):
    service, _ = mix_service
    app = service[1]
    refresh(service)
    old = app.state.mixes.status(uid(service))
    with app.state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE mixes SET running=1 WHERE user_id=?", (uid(service),)
        )
        connection.execute(
            "UPDATE mixes SET next_attempt=? WHERE user_id!=?",
            (time.time() + 86400, uid(service)),
        )

    async def restart():
        await app.state.mixes.start()
        status = app.state.mixes.status(uid(service))
        assert status["snapshot"] == old["snapshot"]
        assert status["profile"] == old["profile"]
        assert not status["running"] and status["error"]
        assert status["next_attempt"] > time.time() + 50
        await app.state.mixes.close()

    run(service, restart)


def test_actual_worker_wakes_and_generated_track_plays(mix_service):
    service, _ = mix_service
    client, app = service[:2]
    with app.state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE mixes SET next_attempt=?", (time.time() + 86400,)
        )

    async def scenario():
        await app.state.mixes.start()
        app.state.mixes.request(uid(service))
        for _ in range(400):
            if app.state.mixes.status(uid(service))["updated"]:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Mix worker did not publish")
        await app.state.mixes.close()

    run(service, scenario)
    song = tracks(service, "Discover")[0]
    response = get(service, "stream", id=song["id"])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/")
    assert response.content
    assert not rows(service, "favorites") and not rows(service, "save_jobs")


def test_relink_same_target_keeps_snapshot_and_revocation_keeps_playlists(
    mix_service,
):
    service, _ = mix_service
    app = service[1]
    refresh(service)
    old = tracks(service, "Discover")
    app.state.mixes.linked(uid(service))
    assert tracks(service, "Discover") == old
    with app.state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE users SET link_status='relink',"
            "lastfm_secret=NULL WHERE id=?",
            (uid(service),),
        )
        connection.execute(
            "UPDATE mixes SET next_attempt=0 WHERE user_id=?", (uid(service),)
        )
        connection.execute(
            "UPDATE mixes SET next_attempt=? WHERE user_id!=?",
            (time.time() + 86400, uid(service)),
        )
    assert tracks(service, "Discover") == old
    assert not run(service, app.state.mixes.refresh_one)
