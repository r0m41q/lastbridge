import pytest
from support.imports import URL, browser, user
from support.library import run

from lastbridge.shared.errors import StorageError


def test_preview_returns_queue_report_without_provider_request(
    imported_service,
):
    service, imports, _, calls = imported_service
    client = service[0]
    token = browser(service)
    response = client.post(
        "/playlist-imports/preview",
        data={"csrf": token, "url": URL, "scope": "shared"},
    )
    item_id = response.url.path.rsplit("/", 1)[1]
    assert response.status_code == 200 and "Queued" in response.text
    assert calls == [] and "Add shared chart" not in response.text
    assert 'http-equiv="refresh"' in response.text
    assert (
        client.post(
            f"/playlist-imports/{item_id}/confirm", data={"csrf": token}
        ).status_code
        == 404
    )
    assert item_id not in {row["id"] for row in imports.rows(user(service))}
    assert 'href="/admin/import-queue"' in client.get("/admin").text
    queue = client.get("/admin/import-queue")
    assert queue.status_code == 200 and "Queued" in queue.text
    for action, expected in (
        ("pause", "Paused"),
        ("resume", "Queued"),
        ("cancel", "Cancelled"),
        ("retry", "Queued"),
    ):
        response = client.post(
            f"/admin/import-queue/{item_id}/{action}", data={"csrf": token}
        )
        assert response.status_code == 200 and expected in response.text
        assert calls == []
    assert run(service, imports.refresh_one)
    report = client.get(f"/playlist-imports/{item_id}")
    assert "3 playable / 4 entries" in report.text
    assert "4 / 4 processed" in report.text
    assert "3 matched" in report.text and "1 unavailable or unmatched" in (
        report.text
    )
    assert "Add shared chart" in report.text
    assert 'http-equiv="refresh"' not in report.text
    assert "Chart &lt;&amp;&gt;" in client.get("/admin/import-queue").text
    assert not imports.get(user(service), item_id)["ready"]


@pytest.mark.parametrize("action", ["pause", "resume", "cancel", "retry"])
def test_queue_controls_enforce_csrf_origin_and_administrator_role(
    imported_service,
    action,
):
    service, imports, _, _ = imported_service
    client = service[0]
    owner = user(service)
    item_id = run(service, imports.preview, owner, URL, True)
    token = browser(service)
    path = f"/admin/import-queue/{item_id}/{action}"
    assert client.post(path, data={"csrf": ""}).status_code == 403
    assert (
        client.post(
            path,
            data={"csrf": token},
            headers={"origin": "https://other.test"},
        ).status_code
        == 403
    )
    listener_token = browser(service, "alice")
    assert client.get("/admin/import-queue").status_code == 403
    assert client.post(path, data={"csrf": listener_token}).status_code == 403
    assert client.get(f"/playlist-imports/{item_id}").status_code == 404
    assert (
        client.post(
            f"/playlist-imports/{item_id}/{action}",
            data={"csrf": listener_token},
        ).status_code
        == 404
    )
    assert imports.get(owner, item_id)["state"] == "queued"


def test_private_queue_isolation_owner_controls_and_unlinked_admin_access(
    imported_service,
):
    service, imports, _, _ = imported_service
    client = service[0]
    alice = user(service, "alice")
    private = run(service, imports.preview, alice, URL)
    admin = user(service)
    assert imports.queue(alice)[0]["id"] == private
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE users SET link_status='unlinked' WHERE username='admin'"
        )
    token = browser(service)
    response = client.get("/admin/import-queue")
    assert response.status_code == 200 and "Private import" in response.text
    assert f'href="/playlist-imports/{private}"' not in response.text
    # Administration can stop work without exposing another user's report.
    assert client.get(f"/playlist-imports/{private}").status_code == 404
    assert (
        client.post(
            f"/admin/import-queue/{private}/pause", data={"csrf": token}
        ).status_code
        == 200
    )
    alice_token = browser(service, "alice")
    assert (
        client.post(
            f"/playlist-imports/{private}/resume", data={"csrf": alice_token}
        ).status_code
        == 200
    )
    assert imports.get(alice, private)["state"] == "queued"
    assert admin["id"] != alice["id"]


def test_queue_pagination_and_invalid_actions(imported_service):
    service, imports, _, _ = imported_service
    owner = user(service)
    for index in range(51):
        run(service, imports.preview, owner, URL + str(index), True)
    token = browser(service)
    client = service[0]
    first = client.get("/admin/import-queue")
    second = client.get("/admin/import-queue?offset=50")
    assert first.status_code == second.status_code == 200
    assert first.text.count("YouTube playlist</a>") == 50
    assert second.text.count("YouTube playlist</a>") == 1
    assert 'href="?offset=50"' in first.text
    assert 'href="?offset=0"' in second.text
    item_id = imports.queue(owner)[0]["id"]
    assert (
        client.post(
            f"/admin/import-queue/{item_id}/resume", data={"csrf": token}
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/admin/import-queue/{item_id}/unknown", data={"csrf": token}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/admin/import-queue/missing/pause", data={"csrf": token}
        ).status_code
        == 404
    )
    assert client.get("/admin/import-queue?offset=-1").status_code == 422


def test_queue_storage_failure_is_reported_without_losing_work(
    imported_service,
    monkeypatch,
):
    service, imports, _, _ = imported_service
    token = browser(service)
    client = service[0]

    def unavailable(*args, **kwargs):
        raise StorageError()

    monkeypatch.setattr(imports.repository, "enqueue", unavailable)
    response = client.post(
        "/playlist-imports/preview", data={"csrf": token, "url": URL}
    )
    assert response.status_code == 503
    assert "storage unavailable" in response.text
    monkeypatch.setattr(imports.repository, "queue", unavailable)
    assert client.get("/admin/import-queue").status_code == 503
