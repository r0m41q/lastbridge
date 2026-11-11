import pytest
from support.accounts import csrf, login
from support.imports import URL, browser, user


def test_browser_preview_confirm_csrf_roles_and_escaping(imported_service):
    service, imports, _, _ = imported_service
    client = service[0]
    token = browser(service)
    assert (
        client.post("/playlist-imports/preview", data={"url": URL}).status_code
        == 403
    )
    assert (
        client.post(
            "/playlist-imports/preview",
            data={"csrf": token, "url": URL},
            headers={"origin": "https://foreign.test"},
        ).status_code
        == 403
    )
    response = client.post(
        "/playlist-imports/preview",
        data={
            "csrf": token,
            "url": URL,
            "scope": "shared",
        },
    )
    assert response.status_code == 200
    assert "Chart &lt;&amp;&gt;" in response.text
    assert "3 playable / 4 entries" in response.text
    assert "Unavailable" in response.text
    item_id = response.url.path.rsplit("/", 1)[1]
    alice_csrf = browser(service, "alice")
    assert client.get("/playlist-imports/" + item_id).status_code == 404
    assert (
        client.post(
            f"/playlist-imports/{item_id}/confirm",
            data={
                "csrf": alice_csrf,
            },
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/playlist-imports/preview",
            data={
                "csrf": alice_csrf,
                "url": URL,
                "scope": "shared",
            },
        ).status_code
        == 403
    )
    token = browser(service)
    assert (
        client.post(
            f"/playlist-imports/{item_id}/confirm",
            data={
                "csrf": token,
            },
        ).status_code
        == 200
    )
    assert "Shared chart" in client.get("/playlists").text
    browser(service, "alice")
    report = client.get("/playlist-imports/" + item_id)
    assert report.status_code == 200
    assert 'type="submit">Refresh' not in report.text
    assert (
        client.post(
            f"/playlist-imports/{item_id}/delete",
            data={
                "csrf": csrf(client.get("/playlists")),
            },
        ).status_code
        == 403
    )
    assert imports.get(user(service), item_id)["ready"]


def test_unlinked_admin_can_manage_shared_chart(imported_service):
    service, imports, state, calls = imported_service
    client, app, _, _ = service
    with app.state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE users SET link_status='unlinked',"
            "lastfm_name=NULL,lastfm_secret=NULL WHERE username='admin'"
        )
    token = browser(service)
    assert user(service)["link_status"] == "unlinked"
    assert 'href="/playlists"' in client.get("/admin").text
    page = client.get("/playlists")
    assert page.status_code == 200
    assert 'href="/playlists" aria-current="page"' in page.text
    assert 'value="shared"' in page.text
    response = client.post(
        "/playlist-imports/preview",
        data={"csrf": token, "url": URL, "scope": "shared"},
    )
    assert response.status_code == 200
    assert "Add shared chart" in response.text
    item_id = response.url.path.rsplit("/", 1)[1]
    assert not imports.get(user(service), item_id)["ready"]
    assert (
        client.post(
            f"/playlist-imports/{item_id}/confirm", data={"csrf": ""}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/playlist-imports/{item_id}/confirm",
            data={"csrf": token},
            headers={"origin": "https://foreign.test"},
        ).status_code
        == 403
    )
    response = client.post(
        f"/playlist-imports/{item_id}/confirm", data={"csrf": token}
    )
    assert response.status_code == 200
    assert imports.get(user(service), item_id)["ready"]
    assert "Chart &lt;&amp;&gt;" in client.get("/admin").text
    state["title"] = "Updated shared chart"
    response = client.post(
        f"/playlist-imports/{item_id}/refresh", data={"csrf": token}
    )
    assert response.status_code == 200
    assert "Updated shared chart" in response.text
    assert len(calls) == 2
    assert (
        client.post(
            f"/playlist-imports/{item_id}/delete", data={"csrf": token}
        ).status_code
        == 200
    )
    assert imports.rows(user(service)) == []
    assert user(service)["lastfm_secret"] is None


@pytest.mark.parametrize(
    "path",
    [
        "/playlists",
        "/playlist-imports/example",
        "/playlist-imports/preview",
        "/playlist-imports/example/confirm",
        "/playlist-imports/example/refresh",
        "/playlist-imports/example/delete",
        "/playlists/create",
        "/playlists/example/delete",
    ],
)
def test_unlinked_listener_cannot_access_playlist_browser(service, path):
    client = service[0]
    login(client, "alice")
    token = csrf(client.get("/"))
    if path in ("/playlists", "/playlist-imports/example"):
        response = client.get(path)
    else:
        response = client.post(path, data={"csrf": token})
    assert response.status_code == 403
    assert response.json() == {"detail": "Link Last.fm first"}
