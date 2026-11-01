from support.accounts import csrf
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
