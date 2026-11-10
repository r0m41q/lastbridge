import asyncio
import hashlib
import time
from urllib.parse import parse_qs

import httpx
import pytest
from support.accounts import PARAMS, csrf, login
from support.build_accounts import make_accounts
from support.catalog import body, get, search
from support.sync import (
    import_steps,
    love_steps,
    page,
    rows,
    run,
    uid,
)

from lastbridge.shared.errors import StorageError


@pytest.mark.parametrize("http_method", ["GET", "POST"])
def test_subsonic_song_love_and_unlove_signed_delivery(
    sync_service, http_method
):
    service = sync_service
    client, app, _, keys = service
    song = search(service)["song"][0]
    seen = []

    def handler(request):
        assert request.method == "POST"
        assert not request.url.query
        params = {
            k: v[0] for k, v in parse_qs(request.content.decode()).items()
        }
        assert params["method"] in ("track.love", "track.unlove")
        assert params["artist"] == song["artist"]
        assert params["track"] == song["title"]
        assert params["sk"] == "admin-session"
        signed = {
            k: v for k, v in params.items() if k not in ("api_sig", "format")
        }
        message = "".join(k + v for k, v in sorted(signed.items())) + "2" * 32
        assert (
            params["api_sig"]
            == hashlib.md5(message.encode(), usedforsecurity=False).hexdigest()
        )
        seen.append(params["method"])
        return httpx.Response(200, json={"lfm": {"status": "ok"}})

    for method in ("star", "unstar"):
        params = PARAMS | {"apiKey": keys["admin"], "id": song["id"]}
        response = (
            client.get("/rest/" + method, params=params)
            if http_method == "GET"
            else client.post("/rest/" + method, data=params)
        )
        assert body(response)["status"] == "ok"
        assert rows(service, "love_outbox")[0]["state"] == "pending"
        love_steps(service, handler)
        assert rows(service, "love_outbox")[0]["state"] == "sent"
    assert seen == ["track.love", "track.unlove"]
    assert not app.state.services.audio.provider.calls
    assert not rows(service)  # Loves never create scrobbles.


def test_rapid_changes_coalesce_and_duplicate_stars_do_not_resend(
    sync_service,
):
    service = sync_service
    song = search(service)["song"][0]
    for method in ("star", "star", "unstar", "star"):
        get(service, method, id=song["id"])
    (job,) = rows(service, "love_outbox")
    assert job["enabled"] == 1 and job["revision"] == 3
    seen = []

    def handler(request):
        seen.append(parse_qs(request.content.decode())["method"][0])
        return httpx.Response(200, json={})

    love_steps(service, handler)
    get(service, "star", id=song["id"])
    love_steps(service, handler)
    assert seen == ["track.love"]


@pytest.mark.parametrize("failure", [False, True])
def test_new_unlike_survives_inflight_love_completion(sync_service, failure):
    service = sync_service
    song = search(service)["song"][0]
    get(service, "star", id=song["id"])
    sync = service[1].state.services.sync
    seen = []

    async def handler(request):
        seen.append(parse_qs(request.content.decode())["method"][0])
        if len(seen) == 1:
            await service[1].state.services.library.change(
                uid(service), [(song["id"], "song")], False
            )
            assert sync.repository.claim_love() is None
            if failure:
                raise httpx.ReadTimeout("private provider message")
        return httpx.Response(200, json={})

    love_steps(service, handler)
    (job,) = rows(service, "love_outbox")
    assert job["state"] == "pending" and job["enabled"] == 0
    assert job["error"] is None and job["next_attempt"] == 0
    love_steps(service, handler)
    assert seen == ["track.love", "track.unlove"]
    assert rows(service, "love_outbox")[0]["state"] == "sent"


@pytest.mark.parametrize(
    "failure",
    ["connect", "timeout", "malformed", "unknown", 500, 8, 11, 16, 29],
)
def test_temporary_love_failures_retry_with_backoff(sync_service, failure):
    service = sync_service
    song = search(service)["song"][0]
    get(service, "star", id=song["id"])

    def handler(request):
        if failure == "connect":
            raise httpx.ConnectError("private provider message")
        if failure == "timeout":
            raise httpx.ReadTimeout("private provider message")
        if failure == "malformed":
            return httpx.Response(200, text="private invalid response")
        if failure == "unknown":
            return httpx.Response(200, json={"unknown": "private"})
        if failure == 500:
            return httpx.Response(500)
        return httpx.Response(
            200, json={"error": failure, "message": "private"}
        )

    love_steps(service, handler)
    (job,) = rows(service, "love_outbox")
    assert job["state"] == "pending" and job["attempts"] == 1
    assert job["next_attempt"] > time.time()
    assert "private" not in job["error"]
    assert service[1].state.services.library.stars(uid(service))
    assert service[1].state.services.sync.repository.claim_love() is None
    with service[1].state.services.database.transaction() as connection:
        connection.execute("UPDATE love_outbox SET next_attempt=0")
    love_steps(service, lambda request: httpx.Response(200, json={}))
    assert rows(service, "love_outbox")[0]["state"] == "sent"


def test_invalid_session_blocks_and_original_account_relink_resumes(
    sync_service,
):
    service = sync_service
    song = search(service)["song"][0]
    get(service, "star", id=song["id"])
    love_steps(service, lambda request: httpx.Response(200, json={"error": 9}))
    services = service[1].state.services
    assert services.accounts.user("admin")["link_status"] == "relink"
    assert rows(service, "love_outbox")[0]["state"] == "blocked"
    with services.database.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='another-account',"
            "lastfm_secret=?,link_status='linked' WHERE id=?",
            (services.accounts.encrypt("another-session"), uid(service)),
        )
    services.sync.linked(uid(service))
    services.sync.control(uid(service), "retry-loves")
    assert services.sync.repository.claim_love() is None
    with services.database.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='ADMIN-FM',lastfm_secret=? "
            "WHERE id=?",
            (services.accounts.encrypt("new-session"), uid(service)),
        )
    services.sync.linked(uid(service))
    seen = []

    def handler(request):
        seen.append(parse_qs(request.content.decode())["sk"][0])
        return httpx.Response(200, json={})

    love_steps(service, handler)
    assert seen == ["new-session"]
    assert rows(service, "love_outbox")[0]["state"] == "sent"


def test_account_change_before_dispatch_never_sends_old_intent(sync_service):
    service = sync_service
    song = search(service)["song"][0]
    get(service, "star", id=song["id"])
    services = service[1].state.services
    with services.database.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='other-account' WHERE id=?",
            (uid(service),),
        )

    def handler(request):
        pytest.fail("Must not deliver an old intent to a different account")

    love_steps(service, handler)
    assert rows(service, "love_outbox")[0]["state"] == "blocked"


def test_failed_loves_retry_privately_through_browser(sync_service):
    service = sync_service
    client, app, _, keys = service
    song = search(service)["song"][0]
    for name in ("admin", "alice"):
        get(service, "star", id=song["id"], apiKey=keys[name])
    love_steps(
        service, lambda request: httpx.Response(200, json={"error": 6}), 2
    )
    assert {job["state"] for job in rows(service, "love_outbox")} == {"failed"}
    login(client)
    view = client.get("/lastfm/sync")
    assert (
        "Song likes" in view.text
        and "Instant Crush &lt;studio&gt;" in view.text
    )
    assert "alice-fm" not in view.text and "admin-session" not in view.text
    assert (
        client.post(
            "/lastfm/sync/retry-loves", data={"csrf": "bad"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/lastfm/sync/retry-loves",
            data={"csrf": csrf(view)},
            follow_redirects=False,
        ).status_code
        == 303
    )
    states = {
        job["user_id"]: job["state"] for job in rows(service, "love_outbox")
    }
    assert states == {uid(service): "pending", uid(service, "alice"): "failed"}
    seen = []

    def handler(request):
        seen.append(parse_qs(request.content.decode())["sk"][0])
        return httpx.Response(200, json={})

    love_steps(service, handler)
    assert seen == ["admin-session"]
    assert app.state.services.sync.loves(uid(service))[0]["state"] == "sent"


def test_import_does_not_echo_and_unlove_uses_original_lastfm_credit(
    sync_service,
):
    service = sync_service
    services = service[1].state.services
    services.sync.control(uid(service, "alice"), "pause")
    song = search(service)["song"][0]
    track = (song["artist"], song["title"])
    import_steps(
        service, lambda request: httpx.Response(200, json=page([track])), 3
    )
    assert not rows(service, "love_outbox")
    assert services.library.stars(uid(service))
    # Original Last.fm names may differ from the provider's display credit.
    with services.database.transaction() as connection:
        connection.execute(
            "UPDATE loved_items SET artist='Original artist',"
            "title='Original title'"
        )
    get(service, "unstar", id=song["id"])
    (job,) = rows(service, "love_outbox")
    assert (job["artist"], job["title"], job["enabled"]) == (
        "Original artist",
        "Original title",
        0,
    )


def test_album_and_artist_hearts_do_not_export_song_loves(sync_service):
    service = sync_service
    song = search(service)["song"][0]
    for field in ("albumId", "artistId"):
        for method in ("star", "unstar"):
            assert (
                body(get(service, method, **{field: song[field]}))["status"]
                == "ok"
            )
    assert not rows(service, "love_outbox")


@pytest.mark.parametrize("phase", ["record", "schedule"])
def test_outbox_write_failure_rolls_back_favorite_and_download_intent(
    sync_service, monkeypatch, phase
):
    service = sync_service
    services = service[1].state.services
    song = search(service)["song"][0]

    def unavailable(*args):
        raise StorageError("Test storage failure")

    target = (
        services.library.repository.love_intents
        if phase == "record"
        else services.library.repository
    )
    monkeypatch.setattr(target, phase, unavailable)

    async def scenario():
        with pytest.raises(StorageError):
            await services.library.change(
                uid(service), [(song["id"], "song")], True
            )

    run(service, scenario)
    assert not services.library.stars(uid(service))
    assert not rows(service, "save_jobs")
    assert not rows(service, "love_outbox")


def test_failed_audio_reconciliation_preserves_love_intent(
    sync_service, monkeypatch
):
    service = sync_service
    services = service[1].state.services
    song = search(service)["song"][0]

    async def unavailable():
        raise OSError("Test storage failure")

    monkeypatch.setattr(services.library, "reconcile", unavailable)

    async def scenario():
        with pytest.raises(OSError):
            await services.library.change(
                uid(service), [(song["id"], "song")], True
            )

    run(service, scenario)
    assert services.library.stars(uid(service))
    love_steps(service, lambda request: httpx.Response(200, json={}))
    assert rows(service, "love_outbox")[0]["state"] == "sent"


def test_restart_recovers_running_latest_choice_without_reexporting_sent(
    sync_service,
):
    service = sync_service
    services = service[1].state.services
    song = search(service)["song"][0]
    get(service, "star", id=song["id"])
    assert services.sync.repository.claim_love() is not None
    get(service, "unstar", id=song["id"])
    services.sync.repository.startup()
    (job,) = rows(service, "love_outbox")
    assert job["state"] == "pending" and job["enabled"] == 0
    love_steps(service, lambda request: httpx.Response(200, json={}))
    services.sync.repository.startup()
    assert services.sync.repository.claim_love() is None


def test_worker_sends_song_loves_and_shutdown_cancels_inflight(sync_service):
    service = sync_service
    services = service[1].state.services
    song = search(service)["song"][0]
    get(service, "star", id=song["id"])

    async def scenario():
        started = asyncio.Event()

        async def handler(request):
            started.set()
            await asyncio.Event().wait()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            services.sync.remote.client = client
            services.sync.control(uid(service), "pause")
            services.sync.control(uid(service, "alice"), "pause")
            await services.sync.start()
            try:
                await asyncio.wait_for(started.wait(), 2)
                assert rows(service, "love_outbox")[0]["state"] == "running"
            finally:
                await services.sync.close()
            assert services.sync.tasks == []
        services.sync.repository.startup()
        assert rows(service, "love_outbox")[0]["state"] == "pending"

    run(service, scenario)


def test_v15_upgrade_preserves_favorites_without_exporting_history(
    sync_service,
):
    service = sync_service
    song = search(service)["song"][0]
    get(service, "star", id=song["id"])
    database = service[1].state.services.database
    with database.transaction() as connection:
        connection.execute("DROP TABLE love_outbox")
        connection.execute("PRAGMA user_version=15")
    make_accounts(database.path.parent)
    assert not rows(service, "love_outbox")
    assert service[1].state.services.library.stars(uid(service))
    assert len(rows(service, "save_jobs")) == 1
    get(service, "star", id=song["id"])
    assert rows(service, "love_outbox")[0]["state"] == "pending"
