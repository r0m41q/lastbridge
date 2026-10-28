import asyncio
import copy
import hashlib
import time
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from test_accounts import PASSWORD, csrf, drop_sync_tables, login
from test_audio import AUDIO, FakeAudio
from test_catalog import SONG, FakeProvider, body, get, search

from lastbridge.accounts import Accounts
from lastbridge.app import create_app
from lastbridge.catalog import Catalog, CatalogError
from lastbridge.config import Settings
from lastbridge.lastfm import Lastfm
from lastbridge.recordings import recording_key


def accepted(code=0):
    return {
        "scrobbles": {
            "@attr": {
                "accepted": str(int(code == 0)),
                "ignored": str(int(code != 0)),
            },
            "scrobble": {
                "ignoredMessage": {"code": str(code), "#text": "secret"}
            },
        }
    }


def page(tracks, current=1, total=1):
    return {
        "lovedtracks": {
            "@attr": {"page": str(current), "totalPages": str(total)},
            "track": [
                {"artist": {"name": artist}, "name": title}
                for artist, title in tracks
            ],
        }
    }


@pytest.fixture
def sync_service(tmp_path, monkeypatch):
    async def taste_rows(self, client, method, **parameters):
        return []

    monkeypatch.setattr(Lastfm, "taste_rows", taste_rows)
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            public_url="http://testserver",
            lastfm_api_key="1" * 32,
            lastfm_api_secret="2" * 32,
        )
    )
    with TestClient(app) as client:
        client.portal.call(app.state.mixes.close)
        client.portal.call(app.state.sync.close)
        client.portal.call(app.state.library.close)
        keys = {}
        for index, name in enumerate(("admin", "alice")):
            app.state.accounts.create_user(
                name, PASSWORD, bootstrap=index == 0
            )
            user = app.state.accounts.user(name)
            with app.state.accounts.transaction() as connection:
                connection.execute(
                    "UPDATE users SET lastfm_name=?,lastfm_secret=?,"
                    "link_status='linked' WHERE id=?",
                    (
                        name + "-fm",
                        app.state.accounts.encrypt(name + "-session"),
                        user["id"],
                    ),
                )
            keys[name] = app.state.accounts.rotate_client_secret(user["id"])
            app.state.sync.linked(user["id"])
        app.state.catalog.provider = FakeProvider()
        app.state.audio.provider = FakeAudio()
        yield client, app, app.state.catalog.provider, keys


def run(service, function, *args):
    return service[0].portal.call(function, *args)


def uid(service, name="admin"):
    return service[1].state.accounts.user(name)["id"]


def rows(service, table="scrobbles"):
    with service[1].state.accounts.connection() as connection:
        return [
            dict(row) for row in connection.execute(f"SELECT * FROM {table}")
        ]


def due(service):
    with service[1].state.accounts.transaction() as connection:
        connection.execute("UPDATE love_imports SET next_attempt=0")
        connection.execute("UPDATE loved_items SET next_attempt=0")
        connection.execute("UPDATE scrobbles SET next_attempt=0")


def transport(service, handler):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            service[1].state.sync.client = client
            await service[1].state.sync.deliver_one()

    return run(service, scenario)


def report(service, song=None, **params):
    song = song or search(service)["song"][0]
    return get(
        service,
        "scrobble",
        id=song["id"],
        time=int((time.time() - 180) * 1000),
        **params,
    )


def test_reports_deduplicate_atomically_and_keep_private_history(sync_service):
    service = sync_service
    client, app, _, keys = service
    song = search(service)["song"][0]
    stamp = int((time.time() - 180) * 1000)
    for _ in range(2):
        assert (
            body(get(service, "scrobble", id=song["id"], time=stamp))["status"]
            == "ok"
        )
    assert len(rows(service)) == 1
    assert rows(service)[0]["timestamp"] == stamp // 1000
    assert (
        body(get(service, "getSong", id=song["id"]))["song"]["playCount"] == 1
    )
    assert (
        "playCount"
        not in body(
            get(service, "getSong", id=song["id"], apiKey=keys["alice"])
        )["song"]
    )
    assert "playCount" not in app.state.catalog.read(song["id"])["document"]
    assert (
        body(
            get(
                service,
                "scrobble",
                id=song["id"],
                time=stamp,
                apiKey=keys["alice"],
            )
        )["status"]
        == "ok"
    )
    assert len(rows(service)) == 2
    params = [
        ("apiKey", keys["admin"]),
        ("v", "1.16.1"),
        ("c", "test"),
        ("f", "json"),
        ("id", song["id"]),
        ("id", "missing"),
        ("time", str(stamp)),
        ("time", str(stamp)),
    ]
    assert (
        body(client.get("/rest/scrobble", params=params))["error"]["code"]
        == 70
    )
    assert len(rows(service)) == 2
    response = client.post(
        "/rest/scrobble.view",
        content=urlencode(params[:-3]),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert body(response)["status"] == "ok"
    assert client.head("/rest/scrobble", params=dict(params)).content == b""
    assert len(rows(service)) == 2


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"id": ""},
        {"id": "bad"},
        {"id": "SONG", "time": "x"},
        {"id": "SONG", "time": "-1"},
        {"id": "SONG", "time": "999999999999999"},
        {"id": "SONG", "submission": "yes"},
    ],
)
def test_invalid_playback_reports_do_not_write(sync_service, params):
    song = search(sync_service)["song"][0]
    params = {k: song["id"] if v == "SONG" else v for k, v in params.items()}
    assert body(get(sync_service, "scrobble", **params))["status"] == "failed"
    assert rows(sync_service) == []


def test_feishin_start_and_submission_without_time(sync_service):
    service = sync_service
    song = search(service)["song"][0]
    assert (
        body(get(service, "scrobble", id=song["id"], submission="false"))[
            "status"
        ]
        == "ok"
    )
    assert rows(service) == []
    start = int(time.time()) - 180
    with service[1].state.accounts.transaction() as connection:
        connection.execute("UPDATE playback_starts SET timestamp=?", (start,))
    for _ in range(2):
        assert (
            body(get(service, "scrobble", id=song["id"], submission="true"))[
                "status"
            ]
            == "ok"
        )
    assert len(rows(service)) == 1
    assert rows(service)[0]["timestamp"] == start
    # Without a start marker, receipt time and a 30-second retry window apply.
    other = service[1].state.catalog.song(SONG | {"videoId": "othertrack1"})
    get(service, "scrobble", id=other["id"])
    get(service, "scrobble", id=other["id"])
    assert len(rows(service)) == 2


def test_signed_delivery_uses_each_users_session_and_ignored_codes(
    sync_service,
):
    service = sync_service
    calls = []
    report(service)
    report(service, apiKey=service[3]["alice"])

    def handler(request):
        values = {
            k: v[0] for k, v in parse_qs(request.content.decode()).items()
        }
        calls.append(values)
        assert request.method == "POST" and request.url.scheme == "https"
        assert not request.url.query
        signed = {
            k: v for k, v in values.items() if k not in ("api_sig", "format")
        }
        message = "".join(k + v for k, v in sorted(signed.items())) + "2" * 32
        assert (
            values["api_sig"]
            == hashlib.md5(message.encode(), usedforsecurity=False).hexdigest()
        )
        return httpx.Response(200, json=accepted(0 if len(calls) == 1 else 3))

    transport(service, handler)
    transport(service, handler)
    assert {call["sk"] for call in calls} == {"admin-session", "alice-session"}
    assert [r["state"] for r in rows(service)] == ["sent", "ignored"]
    assert all("secret" not in (r["error"] or "") for r in rows(service))
    assert (
        body(get(service, "getUser", username="admin"))["user"][
            "scrobblingEnabled"
        ]
        is True
    )


@pytest.mark.parametrize(
    "code, expected",
    [
        (11, "pending"),
        (16, "pending"),
        (29, "pending"),
        (6, "failed"),
        (9, "blocked"),
    ],
)
def test_lastfm_error_retries_and_relink_leave_local_playback_usable(
    sync_service, code, expected
):
    service = sync_service
    report(service)
    transport(
        service,
        lambda r: httpx.Response(
            400, json={"error": code, "message": "secret"}
        ),
    )
    job = rows(service)[0]
    assert job["state"] == expected and job["attempts"] == 1
    assert "secret" not in job["error"]
    assert job["next_attempt"] > time.time()
    if code == 9:
        assert (
            service[1].state.accounts.user("admin")["link_status"] == "relink"
        )
    song = search(service)["song"][0]
    assert (
        service[0]
        .get(
            "/rest/stream",
            params={
                "id": song["id"],
                "apiKey": service[3]["admin"],
                "v": "1.16.1",
                "c": "test",
            },
        )
        .content
        == AUDIO
    )
    assert len(rows(service)) == 1


@pytest.mark.parametrize(
    "kind, expected",
    [
        ("connect", "pending"),
        ("read", "uncertain"),
        ("write", "uncertain"),
        ("malformed", "uncertain"),
        ("server", "uncertain"),
    ],
)
def test_ambiguous_delivery_requires_explicit_retry(
    sync_service, kind, expected
):
    service = sync_service
    report(service)

    def handler(request):
        if kind == "connect":
            raise httpx.ConnectError("secret", request=request)
        if kind == "read":
            raise httpx.ReadTimeout("secret", request=request)
        if kind == "write":
            raise httpx.WriteError("secret", request=request)
        return httpx.Response(503 if kind == "server" else 200, json={})

    transport(service, handler)
    assert rows(service)[0]["state"] == expected
    assert "secret" not in rows(service)[0]["error"]
    service[1].state.sync.control(uid(service), "retry-scrobbles")
    assert rows(service)[0]["state"] == expected
    if expected == "uncertain":
        service[1].state.sync.control(uid(service), "retry-uncertain")
        transport(service, lambda r: httpx.Response(200, json=accepted()))
        assert rows(service)[0]["state"] == "sent"


def test_restart_marks_inflight_uncertain_and_preserves_pending(sync_service):
    service = sync_service
    report(service)
    report(service, apiKey=service[3]["alice"])
    with service[1].state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE scrobbles SET state='running' WHERE user_id=?",
            (uid(service),),
        )

    async def restart():
        service[1].state.lastfm.api_key = ""
        await service[1].state.sync.start()
        await service[1].state.sync.close()

    run(service, restart)
    assert [r["state"] for r in rows(service)] == ["uncertain", "pending"]
    reopened = Accounts(service[1].state.config.data_dir)
    with reopened.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM scrobbles").fetchone()[0]
            == 2
        )


def test_different_linked_account_never_receives_old_scrobbles(sync_service):
    service = sync_service
    report(service)
    with service[1].state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='different' WHERE id=?",
            (uid(service),),
        )
    service[1].state.sync.linked(uid(service))
    calls = []
    transport(service, lambda r: calls.append(r))
    assert not calls and rows(service)[0]["state"] == "blocked"
    service[1].state.sync.control(uid(service), "retry-scrobbles")
    assert rows(service)[0]["state"] == "blocked"


def import_steps(service, handler, count=1):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            service[1].state.sync.client = client
            for _ in range(count):
                due(service)
                await service[1].state.sync.import_one()

    run(service, scenario)


def test_paginated_import_matches_privately_and_resumes_after_pause(
    sync_service,
):
    service = sync_service
    app = service[1]
    app.state.sync.control(uid(service, "alice"), "pause")
    track = (SONG["artists"][0]["name"], SONG["title"])
    seen = []

    def handler(request):
        assert request.url.params["method"] == "user.getLovedTracks"
        current = int(request.url.params["page"])
        seen.append(current)
        return httpx.Response(
            200,
            json=page(
                [track] if current == 1 else [track, ("Nobody", "Missing")],
                current,
                2,
            ),
        )

    import_steps(service, handler)
    assert seen == [1] and rows(service, "love_imports")[0]["next_page"] == 2
    app.state.sync.control(uid(service), "pause")
    import_steps(service, handler, 2)
    assert seen == [1] and not app.state.library.stars(uid(service))
    app.state.sync.control(uid(service), "resume")
    import_steps(service, handler, 6)
    assert seen == [1, 2]
    job, counts, items, _ = app.state.sync.snapshot(uid(service))
    assert job["state"] == "complete" and counts == {
        "matched": 1,
        "unmatched": 1,
    }
    assert items[0]["title"] == "Missing"
    assert len(app.state.library.stars(uid(service))) == 1
    assert not app.state.library.stars(uid(service, "alice"))
    assert len(app.state.library.downloads(uid(service))) == 1
    assert not app.state.audio.provider.calls
    run(service, app.state.library.process_one)
    assert app.state.library.downloads(uid(service))[0]["state"] == "complete"


def test_refresh_keeps_local_removals_and_allows_explicit_reheart(
    sync_service,
):
    service = sync_service
    app = service[1]
    app.state.sync.control(uid(service, "alice"), "pause")
    track = (SONG["artists"][0]["name"], SONG["title"])

    def handler(request):
        return httpx.Response(200, json=page([track]))

    import_steps(service, handler, 3)
    song = app.state.library.collection(uid(service))["song"][0]
    assert body(get(service, "unstar", id=song["id"]))["status"] == "ok"
    app.state.sync.control(uid(service), "refresh")
    import_steps(service, handler, 3)
    assert app.state.library.stars(uid(service)) == {}
    assert app.state.sync.snapshot(uid(service))[1] == {"skipped": 1}
    assert rows(service, "love_removals")[0]["recording_key"] == recording_key(
        *track
    )
    get(service, "star", id=song["id"])
    assert len(app.state.library.stars(uid(service))) == 1
    assert not rows(service, "love_removals")


def test_conservative_match_refuses_versions_covers_and_ambiguity(
    sync_service,
):
    service = sync_service
    app = service[1]
    original = copy.deepcopy(SONG)
    candidates = [original, original | {"videoId": "othertrack1"}]

    async def fetch(payload):
        return {"songs": candidates}

    app.state.catalog.provider.fetch = fetch
    from lastbridge.providers.base import ProviderError

    with pytest.raises(ProviderError, match="ambiguous_match"):
        run(
            service,
            app.state.catalog.match_loved,
            original["artists"][0]["name"],
            original["title"],
        )
    candidates[:] = [
        original | {"title": original["title"] + " (Live)"},
        original | {"artists": [{"name": "Cover Artist"}]},
    ]
    with pytest.raises(ProviderError, match="no_match"):
        run(
            service,
            app.state.catalog.match_loved,
            original["artists"][0]["name"],
            original["title"],
        )


def test_sync_browser_controls_are_private_escaped_and_csrf_protected(
    sync_service,
):
    service = sync_service
    client, app, _, _ = service
    report(service)
    assert (
        client.get("/lastfm/sync", follow_redirects=False).status_code == 303
    )
    login(client, "alice")
    assert "Instant Crush" not in client.get("/lastfm/sync").text
    login(client)
    view = client.get("/lastfm/sync")
    assert "Instant Crush &lt;studio&gt;" in view.text
    assert "admin-session" not in view.text and "1" * 32 not in view.text
    assert (
        client.post("/lastfm/sync/pause", data={"csrf": "bad"}).status_code
        == 403
    )
    assert (
        client.post(
            "/lastfm/sync/pause", data={"csrf": csrf(view)}
        ).status_code
        == 200
    )
    assert app.state.sync.snapshot(uid(service))[0]["state"] == "paused"
    assert (
        app.state.sync.snapshot(uid(service, "alice"))[0]["state"] == "active"
    )


def test_v3_migration_preserves_favorites_credentials_and_audio_intent(
    sync_service,
):
    service = sync_service
    store = service[1].state.accounts
    song = search(service)["song"][0]
    get(service, "star", id=song["id"])
    with store.transaction() as connection:
        drop_sync_tables(connection)
        connection.execute("PRAGMA user_version=3")
    migrated = Accounts(store.path.parent)
    assert (
        migrated.client_user({"apiKey": service[3]["admin"]})["username"]
        == "admin"
    )
    assert Catalog(migrated).read(song["id"])["document"] == song
    with migrated.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute("SELECT count(*) FROM favorites").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM save_jobs").fetchone()[0]
            == 1
        )


def test_pause_and_refresh_during_page_read_do_not_advance_cursor(
    sync_service,
):
    service = sync_service
    sync = service[1].state.sync
    sync.control(uid(service, "alice"), "pause")

    for action in ("pause", "refresh"):
        sync.control(uid(service), "resume")

        def handler(request, action=action):
            sync.control(uid(service), action)
            return httpx.Response(200, json=page([("Artist", "Track")]))

        import_steps(service, handler)
        assert sync.snapshot(uid(service))[0]["next_page"] == 1
        assert not rows(service, "loved_items")


def test_import_outage_keeps_cursor_and_retries_after_restart(sync_service):
    service = sync_service
    sync = service[1].state.sync
    sync.control(uid(service, "alice"), "pause")
    import_steps(service, lambda r: httpx.Response(503, json={"error": 11}))
    job = sync.snapshot(uid(service))[0]
    assert job["next_page"] == 1 and job["state"] == "active"
    assert job["error"] and job["next_attempt"] > time.time()
    reopened = Accounts(service[1].state.config.data_dir)
    with reopened.connection() as connection:
        assert (
            connection.execute(
                "SELECT next_page FROM love_imports WHERE user_id=?",
                (uid(service),),
            ).fetchone()[0]
            == 1
        )
    import_steps(
        service, lambda r: httpx.Response(200, json=page([], total=0)), 2
    )
    assert sync.snapshot(uid(service))[0]["state"] == "complete"


def test_pause_or_unlike_during_matching_cannot_add_an_import(sync_service):
    service = sync_service
    app = service[1]
    sync = app.state.sync
    sync.control(uid(service, "alice"), "pause")
    track = (SONG["artists"][0]["name"], SONG["title"])

    def handler(request):
        return httpx.Response(200, json=page([track]))

    import_steps(service, handler)
    song = app.state.catalog.song(SONG)

    async def paused(artist, title):
        sync.control(uid(service), "pause")
        return song

    app.state.catalog.match_loved = paused
    import_steps(service, handler)
    assert app.state.library.stars(uid(service)) == {}
    assert sync.snapshot(uid(service))[1] == {"pending": 1}
    sync.control(uid(service), "resume")

    async def removed(artist, title):
        await app.state.library.change(
            uid(service), [(song["id"], "song")], False
        )
        return song

    app.state.catalog.match_loved = removed
    import_steps(service, handler)
    assert app.state.library.stars(uid(service)) == {}
    assert sync.snapshot(uid(service))[1] == {"skipped": 1}


def test_old_session_failure_does_not_revoke_a_new_link(sync_service):
    service = sync_service
    report(service)
    app = service[1]

    def handler(request):
        with app.state.accounts.transaction() as connection:
            connection.execute(
                "UPDATE users SET lastfm_secret=? WHERE id=?",
                (app.state.accounts.encrypt("new-session"), uid(service)),
            )
        app.state.sync.linked(uid(service))
        return httpx.Response(200, json={"error": 9})

    transport(service, handler)
    user = app.state.accounts.user("admin")
    assert user["link_status"] == "linked"
    assert app.state.accounts.decrypt(user["lastfm_secret"]) == "new-session"
    app.state.sync.control(uid(service), "retry-scrobbles")
    transport(service, lambda r: httpx.Response(200, json=accepted()))
    assert rows(service)[0]["state"] == "sent"


def test_cancelled_delivery_is_not_silently_retried(sync_service):
    service = sync_service
    report(service)

    async def scenario():
        entered = asyncio.Event()

        async def handler(request):
            entered.set()
            await asyncio.Event().wait()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            sync = service[1].state.sync
            sync.client = client
            task = asyncio.create_task(sync.deliver_one())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert rows(service)[0]["state"] == "running"
            service[1].state.lastfm.api_key = ""
            await sync.start()
            await sync.close()
            assert rows(service)[0]["state"] == "uncertain"

    run(service, scenario)


def test_older_pending_report_holds_later_reports_but_not_other_user(
    sync_service,
):
    service = sync_service
    song = search(service)["song"][0]
    start = int(time.time()) - 600
    for second in (start, start + 300):
        get(service, "scrobble", id=song["id"], time=second * 1000)
    report(service, apiKey=service[3]["alice"])
    transport(service, lambda r: httpx.Response(503, json={"error": 11}))
    transport(service, lambda r: httpx.Response(200, json=accepted()))
    assert [r["state"] for r in rows(service)] == [
        "pending",
        "pending",
        "sent",
    ]
    due(service)
    transport(service, lambda r: httpx.Response(200, json=accepted()))
    transport(service, lambda r: httpx.Response(200, json=accepted()))
    assert all(r["state"] == "sent" for r in rows(service))


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"lovedtracks": []},
        page([], current=2),
        {
            "lovedtracks": {
                "@attr": {"page": 1, "totalPages": 1},
                "track": [None],
            }
        },
        page([("Artist", "Track")], total=0),
    ],
)
def test_malformed_import_page_never_advances_cursor(sync_service, payload):
    service = sync_service
    sync = service[1].state.sync
    sync.control(uid(service, "alice"), "pause")
    import_steps(service, lambda r: httpx.Response(200, json=payload))
    assert sync.snapshot(uid(service))[0]["next_page"] == 1
    assert sync.snapshot(uid(service))[0]["error"]
    assert not rows(service, "loved_items")


def test_short_recordings_count_locally_without_lastfm_delivery(sync_service):
    service = sync_service
    song = service[1].state.catalog.song(SONG | {"duration_seconds": 30})
    report(service, song)
    assert rows(service)[0]["state"] == "ignored"
    calls = []
    transport(service, lambda r: calls.append(r))
    assert calls == []
    assert (
        body(get(service, "getSong", id=song["id"]))["song"]["playCount"] == 1
    )


def test_background_import_bootstraps_existing_link_and_resumes_disk_cursor(
    tmp_path,
    monkeypatch,
):
    directory = tmp_path / "data"
    store = Accounts(directory)
    store.create_user("admin", PASSWORD, bootstrap=True)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='admin-fm',lastfm_secret=?,"
            "link_status='linked'",
            (store.encrypt("session"),),
        )
    calls = []

    async def loved(self, client, name, current):
        calls.append(current)
        return [("Missing", "No match")], 2

    async def unavailable(self, artist, title):
        raise CatalogError()

    monkeypatch.setattr(Lastfm, "loved", loved)
    monkeypatch.setattr(Catalog, "match_loved", unavailable)

    async def empty_taste(self, client, method, **parameters):
        return []

    monkeypatch.setattr(Lastfm, "taste_rows", empty_taste)
    settings = Settings(
        _env_file=None,
        data_dir=directory,
        lastfm_api_key="1" * 32,
        lastfm_api_secret="2" * 32,
    )
    with TestClient(create_app(settings)) as client:

        async def first_page():
            for _ in range(100):
                if client.app.state.sync.snapshot(1)[0]["next_page"] == 2:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("Import worker did not fetch first page")

        client.portal.call(first_page)
        client.app.state.sync.control(1, "pause")
    assert calls == [1]
    with TestClient(create_app(settings)) as client:
        assert client.app.state.sync.snapshot(1)[0]["state"] == "paused"
        assert client.app.state.sync.snapshot(1)[0]["next_page"] == 2
        assert calls == [1]
        client.app.state.sync.control(1, "resume")

        async def second_page():
            for _ in range(200):
                if client.app.state.sync.snapshot(1)[0]["next_page"] == 3:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("Import did not resume its saved cursor")

        client.portal.call(second_page)
        assert calls == [1, 2]


def test_actual_background_delivery_wakes_after_report(sync_service):
    service = sync_service
    sync = service[1].state.sync
    delivered = []

    async def loved(client, name, current):
        return [], 0

    async def scrobble(client, secret, job):
        delivered.append((secret, job["title"]))
        return "sent", None

    service[1].state.lastfm.loved = loved
    service[1].state.lastfm.scrobble = scrobble
    run(service, sync.start)
    report(service)

    async def wait_for_delivery():
        for _ in range(100):
            if rows(service)[0]["state"] == "sent":
                return
            await asyncio.sleep(0.01)
        raise AssertionError("Scrobble worker did not wake")

    run(service, wait_for_delivery)
    run(service, sync.close)
    assert delivered == [("admin-session", SONG["title"])]


def test_form_batch_pairs_times_and_xml_response(sync_service):
    service = sync_service
    first = search(service)["song"][0]
    second = service[1].state.catalog.song(SONG | {"videoId": "othertrack1"})
    stamp = int(time.time()) - 600
    params = [
        ("apiKey", service[3]["admin"]),
        ("v", "1.16.1"),
        ("c", "test"),
        ("id", first["id"]),
        ("id", second["id"]),
        ("time", str(stamp * 1000)),
        ("time", str((stamp + 300) * 1000)),
    ]
    response = service[0].post(
        "/rest/scrobble.view",
        content=urlencode(params),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    from xml.etree.ElementTree import fromstring

    assert fromstring(response.content).attrib["status"] == "ok"
    assert [(r["song_id"], r["timestamp"]) for r in rows(service)] == [
        (first["id"], stamp),
        (second["id"], stamp + 300),
    ]
    missing_time = service[0].get("/rest/scrobble", params=params[:-1])
    assert (
        fromstring(missing_time.content).find("{*}error").attrib["code"]
        == "10"
    )
    assert len(rows(service)) == 2


def test_removal_and_reheart_clear_original_import_credit_key(sync_service):
    service = sync_service
    app = service[1]
    song = app.state.catalog.song(SONG)
    key = recording_key("Daft Punk", SONG["title"])
    with app.state.accounts.transaction() as connection:
        connection.execute(
            "INSERT INTO loved_items(user_id,target,recording_key,"
            "artist,title,"
            "state,song_id) VALUES(?,?,?,?,?,'matched',?)",
            (
                uid(service),
                "admin-fm",
                key,
                "Daft Punk",
                SONG["title"],
                song["id"],
            ),
        )
    get(service, "unstar", id=song["id"])
    assert key in {r["recording_key"] for r in rows(service, "love_removals")}
    get(service, "star", id=song["id"])
    assert not rows(service, "love_removals")


def test_unicode_metadata_is_signed_and_delivered_without_ascii_loss(
    sync_service,
):
    service = sync_service
    song = service[1].state.catalog.song(
        SONG
        | {
            "title": "Судоми",
            "artists": [{"name": "OTOY", "id": "UC_otoy"}],
        }
    )
    report(service, song)

    def handler(request):
        values = {
            k: v[0] for k, v in parse_qs(request.content.decode()).items()
        }
        assert values["artist"] == "OTOY" and values["track"] == "Судоми"
        signed = {
            k: v for k, v in values.items() if k not in ("api_sig", "format")
        }
        message = "".join(k + v for k, v in sorted(signed.items())) + "2" * 32
        assert (
            values["api_sig"]
            == hashlib.md5(message.encode(), usedforsecurity=False).hexdigest()
        )
        return httpx.Response(200, json=accepted())

    transport(service, handler)
    assert rows(service)[0]["state"] == "sent"
