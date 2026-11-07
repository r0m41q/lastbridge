from collections import namedtuple

import pytest
from support.accounts import PASSWORD, csrf, login
from support.catalog import body, get, search
from support.imports import accept, browser, user
from support.library import run


@pytest.mark.parametrize("name,status", [(None, 303), ("alice", 403)])
def test_dashboard_requires_administrator(service, name, status):
    client = service[0]
    if name:
        login(client, name)
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == status
    assert "alice" not in response.text
    assert "Saved audio" not in response.text
    if name:
        assert 'href="/admin"' not in client.get("/").text


def test_admin_can_view_dashboard_before_lastfm_link(service):
    client, app, _ = service
    login(client)
    response = client.get("/admin")
    assert response.status_code == 200
    assert 'href="/admin" aria-current="page"' in response.text
    assert "alice" in response.text and "Saved audio" in response.text
    account = app.state.services.accounts.user("admin")
    for field in ("password_hash", "lastfm_secret", "client_secret"):
        value = account.get(field)
        if value:
            assert value not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "<script" not in response.text


def test_admin_can_update_audio_settings(service):
    client, app, _ = service
    login(client)
    token = csrf(client.get("/admin"))
    values = {
        "csrf": token,
        "provider": "youtube",
        "policy": "opus",
        "cache_bytes": str(16 * 1024 * 1024),
        "file_max_bytes": str(4 * 1024 * 1024),
        "free_reserve_bytes": str(2 * 1024 * 1024),
        "timeout_seconds": "45",
    }

    response = client.post("/admin/audio", data=values, follow_redirects=False)

    assert response.status_code == 303
    assert app.state.services.audio.storage.policy == "opus"
    assert app.state.services.audio.storage.budget == 16 * 1024 * 1024
    assert app.state.services.audio.storage.maximum == 4 * 1024 * 1024
    assert app.state.services.library.policy == "opus"
    assert (
        app.state.services.administration.audio_settings.snapshot().as_dict()
        == {
            "provider": "youtube",
            "policy": "opus",
            "cache_bytes": 16 * 1024 * 1024,
            "file_max_bytes": 4 * 1024 * 1024,
            "free_reserve_bytes": 2 * 1024 * 1024,
            "timeout_seconds": 45,
        }
    )


def test_invalid_audio_settings_are_not_saved(service):
    client, app, _ = service
    login(client)
    token = csrf(client.get("/admin"))
    values = {
        "csrf": token,
        "provider": "youtube",
        "policy": "opus",
        "cache_bytes": str(4 * 1024 * 1024),
        "file_max_bytes": str(4 * 1024 * 1024),
        "free_reserve_bytes": "0",
        "timeout_seconds": "45",
    }

    response = client.post("/admin/audio", data=values)

    assert response.status_code == 409
    assert app.state.services.audio.storage.policy == "aac"
    assert (
        app.state.services.administration.audio_settings.snapshot().policy
        == "aac"
    )


@pytest.mark.parametrize("action", ["downloads", "password", "mixes", "pause"])
def test_normal_user_cannot_operate_admin_controls(service, action):
    client = service[0]
    login(client, "alice")
    response = client.post(
        f"/admin/users/1/{action}",
        data={
            "csrf": csrf(client.get("/")),
            "password": "new-password",
            "confirm": "yes",
        },
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/admin/users/2/downloads",
        "/admin/charts/example/retry",
        "/admin/audio",
    ],
)
def test_admin_actions_require_csrf_and_same_origin(service, path):
    client = service[0]
    login(client)
    assert client.post(path, data={"csrf": "wrong"}).status_code == 403
    token = csrf(client.get("/"))
    assert (
        client.post(
            path,
            data={"csrf": token},
            headers={"Origin": "https://other.test"},
        ).status_code
        == 403
    )


def test_reset_requires_confirmation_and_invalidates_sessions_and_clients(
    sync_service,
):
    client, app, _, keys = sync_service
    accounts = app.state.services.accounts
    alice_token = accounts.login("alice", PASSWORD, "test")
    login(client)
    token = csrf(client.get("/admin"))
    target = accounts.user("alice")["id"]
    url = f"/admin/users/{target}/password"
    data = {"csrf": token, "password": "new-password"}
    assert client.post(url, data=data).status_code == 409
    assert accounts.session(alice_token)
    assert (
        client.post(
            url, data=data | {"confirm": "yes"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert accounts.session(alice_token) is None
    assert accounts.client_user({"apiKey": keys["alice"]}) is None
    assert accounts.user("alice")["link_status"] == "linked"
    assert login(client, "alice", "new-password").status_code == 303


def test_failed_download_retry_queues_only_the_target_users_files(
    saved_service,
):
    service = saved_service
    client, app, _, keys = service
    song = search(service)["song"][0]["id"]
    get(service, "star", id=song, apiKey=keys["alice"])
    database = app.state.services.database
    with database.transaction() as connection:
        connection.execute("UPDATE save_jobs SET state='failed',attempts=3")
    token = browser(service)
    response = client.get("/admin")
    assert "1 failed" in response.text
    assert (
        client.post(
            "/admin/users/1/downloads",
            data={"csrf": token},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert app.state.services.library.downloads(2)[0]["state"] == "failed"
    assert (
        client.post(
            "/admin/users/2/downloads",
            data={"csrf": token},
            follow_redirects=False,
        ).status_code
        == 303
    )
    job = app.state.services.library.downloads(2)[0]
    assert job["state"] == "pending" and job["attempts"] == 0
    assert not app.state.services.audio.provider.calls


def test_mix_retry_pause_resume_and_unlinked_rejection(sync_service):
    client, app, _, _ = sync_service
    login(client)
    token = csrf(client.get("/admin"))
    values = {"csrf": token}
    for action in ("pause", "resume", "mixes"):
        assert (
            client.post(
                f"/admin/users/2/{action}", data=values, follow_redirects=False
            ).status_code
            == 303
        )
    assert app.state.services.sync.snapshot(2)[0]["state"] == "active"
    assert app.state.services.mixes.status(2)["requested"] > 0
    assert client.post("/admin/users/2/mixes", data=values).status_code == 409
    app.state.services.accounts.mark_relink(2)
    assert client.post("/admin/users/2/resume", data=values).status_code == 409
    assert (
        client.post("/admin/users/999/downloads", data=values).status_code
        == 409
    )
    assert (
        client.post("/admin/users/2/unknown", data=values).status_code == 409
    )


def test_shared_chart_retry_is_queued_once_without_touching_private_imports(
    imported_service,
):
    service, imports, state, calls = imported_service
    shared = accept(imported_service, shared=True)
    private = accept(imported_service, name="alice")
    database = service[1].state.services.database
    with database.transaction() as connection:
        connection.execute(
            "UPDATE playlist_imports SET error='failed',"
            "next_attempt=9999999999"
        )
    token = browser(service)
    client = service[0]
    assert "Chart &lt;&amp;&gt;" in client.get("/admin").text
    before = len(calls)
    for item_id in (shared, private):
        assert (
            client.post(
                f"/admin/charts/{item_id}/retry",
                data={"csrf": token},
                follow_redirects=False,
            ).status_code
            == 303
        )
    assert len(calls) == before
    assert imports.get(user(service, "alice"), private)["next_attempt"] > 0
    assert run(service, imports.refresh_one)
    assert len(calls) == before + 1
    assert not run(service, imports.refresh_one)
    assert imports.get(user(service), shared)["error"] is None
    browser(service, "alice")
    assert (
        client.post(
            f"/admin/charts/{shared}/retry",
            data={"csrf": csrf(client.get("/"))},
        ).status_code
        == 403
    )


def test_low_disk_and_transfer_reservations_are_visible(service, monkeypatch):
    client, app, _ = service
    disk = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "lastbridge.features.administration.adapters.filesystem.usage."
        "shutil.disk_usage",
        lambda directory: disk(1000, 990, 10),
    )
    login(client)
    response = client.get("/admin")
    assert response.status_code == 200
    assert "Low disk space" in response.text
    assert "10 B" in response.text
    report = client.portal.call(app.state.services.administration.dashboard)
    assert report["storage"]["low_space"]
    assert report["storage"]["reserved"] == 0


def test_password_reset_preserves_both_users_retention(saved_service):
    service = saved_service
    client, app, _, keys = service
    song = search(service)["song"][0]["id"]
    for name in ("admin", "alice"):
        get(service, "star", id=song, apiKey=keys[name])
    token = browser(service)
    before = run(service, app.state.services.administration.dashboard)
    response = client.post(
        "/admin/users/2/password",
        data={"csrf": token, "password": "replacement", "confirm": "yes"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    after = run(service, app.state.services.administration.dashboard)
    assert [u["retained"] for u in before["users"]] == [
        u["retained"] for u in after["users"]
    ]
    assert get(service, "getSong", id=song).status_code == 200
    assert (
        body(get(service, "star", id=song, apiKey=keys["alice"]))["error"][
            "code"
        ]
        == 44
    )


def test_scrobble_retry_leaves_uncertain_delivery_untouched(sync_service):
    client, app, _, _ = sync_service
    song = search(sync_service)["song"][0]["id"]
    for timestamp in (100000, 200000):
        get(sync_service, "scrobble", id=song, time=timestamp)
    with app.state.services.database.transaction() as connection:
        ids = [r[0] for r in connection.execute("SELECT id FROM scrobbles")]
        assert len(ids) == 2
        connection.execute("UPDATE scrobbles SET state='failed'")
        connection.execute(
            "UPDATE scrobbles SET state='uncertain' WHERE id=?", (ids[0],)
        )
    login(client)
    token = csrf(client.get("/admin"))
    assert (
        client.post(
            "/admin/users/1/retry-scrobbles",
            data={"csrf": token},
            follow_redirects=False,
        ).status_code
        == 303
    )
    with app.state.services.database.connection() as connection:
        states = [
            r[0]
            for r in connection.execute(
                "SELECT state FROM scrobbles ORDER BY id"
            )
        ]
    assert states == ["uncertain", "pending"]


def test_self_password_reset_signs_administrator_out(service):
    client = service[0]
    login(client)
    token = csrf(client.get("/admin"))
    assert (
        client.post(
            "/admin/users/1/password",
            data={"csrf": token, "password": "replacement", "confirm": "yes"},
        ).url.path
        == "/login"
    )
    assert login(client, "admin", "replacement").status_code == 303


def test_admin_create_user_from_dashboard(service):
    client = service[0]
    login(client)
    token = csrf(client.get("/admin"))
    assert (
        client.post(
            "/admin/users",
            data={
                "csrf": token,
                "username": "listener",
                "password": "12345678",
            },
        ).status_code
        == 200
    )
    assert "listener" in client.get("/admin").text
    assert login(client, "listener", "12345678").status_code == 303
    assert client.get("/admin").status_code == 403


def test_storage_failure_returns_fixed_message_without_paths(
    service, monkeypatch
):
    client = service[0]
    login(client)

    def unavailable(directory):
        raise OSError("private path and secret")

    monkeypatch.setattr(
        "lastbridge.features.administration.adapters.filesystem.usage."
        "shutil.disk_usage",
        unavailable,
    )
    response = client.get("/admin")
    assert response.status_code == 503
    assert "Storage unavailable" in response.text
    assert "private path" not in response.text


def test_storage_reports_reserved_capacity_separately(service):
    client, app, _ = service
    storage = app.state.services.audio.storage

    async def snapshot():
        async with storage.lock:
            storage.reserved = 123
        try:
            return await app.state.services.administration.dashboard()
        finally:
            async with storage.lock:
                storage.reserved = 0

    report = client.portal.call(snapshot)
    assert report["storage"]["reserved"] == 123
    assert report["storage"]["saved_bytes"] == 0
    assert report["storage"]["cache_bytes"] == 0
