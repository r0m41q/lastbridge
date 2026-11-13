import pytest
from support.catalog import body, get, search
from support.imports import (
    URL,
    accept,
    accept_lastfm,
    browser,
    user,
)
from support.library import run


def test_local_playlist_rename_preserves_tracks_and_metadata(saved_service):
    service = saved_service
    client, app, _, _ = service
    playlists = app.state.services.playlists
    owner = user(service, "alice")
    song = search(service)["song"][0]["id"]
    item = run(service, playlists.create, owner, "Original", [song, song])
    run(service, playlists.update, owner, item["id"], {"comment": "Notes"})
    before = playlists.get(owner, item["id"])
    token = browser(service, "alice")
    path = f"/playlists/{item['id']}/rename"
    assert f'action="{path}"' in client.get("/playlists").text
    response = client.post(
        path, data={"csrf": token, "name": 'Evening <&> "mix" ♪'}
    )
    assert response.status_code == 200
    assert response.url.path == "/playlists"
    assert 'value="Evening &lt;&amp;&gt; &#34;mix&#34; ♪"' in response.text
    after = playlists.get(owner, item["id"])
    assert after["name"] == 'Evening <&> "mix" ♪'
    for field in ("entry", "comment", "created", "public", "owner"):
        assert after[field] == before[field]
    assert after["changed"] > before["changed"]
    assert (
        body(
            get(
                service,
                "getPlaylist",
                id=item["id"],
                apiKey=service[3]["alice"],
            )
        )["playlist"]
        == after
    )
    token = browser(service)
    assert (
        client.post(path, data={"csrf": token, "name": "Stolen"}).status_code
        == 404
    )
    assert playlists.get(owner, item["id"]) == after


@pytest.mark.parametrize(
    "fixture_name,shared",
    [
        ("imported_service", False),
        ("imported_service", True),
        ("lastfm_imported_service", False),
    ],
)
def test_import_rename_survives_refresh_and_can_reset(
    request, fixture_name, shared
):
    bundle = request.getfixturevalue(fixture_name)
    service, imports, state, calls = bundle
    name = "admin" if shared else "alice"
    owner = user(service, name)
    item_id = (
        accept_lastfm(bundle)
        if fixture_name == "lastfm_imported_service"
        else accept(bundle, name=name, shared=shared)
    )
    token = browser(service, name)
    client = service[0]
    path = f"/playlist-imports/{item_id}/rename"
    page = client.get(f"/playlist-imports/{item_id}")
    assert f'action="{path}"' in page.text
    # The editable form lives outside the header replaced by progress polling.
    model = client.get(page.url, headers={"Accept": "application/json"})
    assert f'action="{path}"' not in model.json()["header"]
    job = imports.repository.claim(item_id)
    before = imports.get(owner, item_id)
    fetched = len(calls)
    response = client.post(
        path, data={"csrf": token, "name": 'My <&> "playlist"'}
    )
    assert response.status_code == 200
    assert response.url.path == f"/playlist-imports/{item_id}"
    assert 'value="My &lt;&amp;&gt; &#34;playlist&#34;"' in response.text
    after = imports.get(owner, item_id)
    assert after["title"] == 'My <&> "playlist"'
    for field in (
        "source",
        "entries",
        "token",
        "state",
        "next_attempt",
        "progress",
        "last_checked",
        "refresh_days",
    ):
        assert after[field] == before[field]
    assert len(calls) == fetched
    if fixture_name == "imported_service":
        state["title"] = "New source name"
    assert run(service, imports.load, job)
    refreshed = imports.get(owner, item_id)
    assert refreshed["title"] == 'My <&> "playlist"'
    assert refreshed["updated"] == after["updated"]
    assert (
        "My &lt;&amp;&gt; &#34;playlist&#34;" in client.get("/playlists").text
    )
    document = body(
        get(service, "getPlaylist", id=item_id, apiKey=service[3][name])
    )["playlist"]
    assert document["name"] == after["title"]
    assert (
        client.post(path, data={"csrf": token, "name": ""}).status_code == 200
    )
    reset = imports.get(owner, item_id)
    assert reset["custom_title"] is None
    assert reset["title"] == (
        "New source name"
        if fixture_name == "imported_service"
        else "Last.fm playlist 12354876"
    )
    token = browser(service, "alice" if shared else "admin")
    page = client.get(f"/playlist-imports/{item_id}")
    assert f'action="{path}"' not in page.text
    assert client.post(
        path, data={"csrf": token, "name": "Stolen"}
    ).status_code == (403 if shared else 404)
    assert imports.get(owner, item_id)["title"] == reset["title"]


@pytest.mark.parametrize("kind", ["local", "private", "shared"])
def test_rename_requires_valid_name_csrf_and_same_origin(
    imported_service, kind
):
    service, imports, _, _ = imported_service
    client = service[0]
    token = browser(service)
    if kind == "local":
        playlists = service[1].state.services.playlists
        item = run(service, playlists.create, user(service), "Original", [])
        item_id = item["id"]
        path = f"/playlists/{item_id}/rename"
    else:
        item_id = accept(imported_service, shared=kind == "shared")
        path = f"/playlist-imports/{item_id}/rename"
    assert client.post(path, data={"name": "Oops"}).status_code == 403
    assert (
        client.post(
            path,
            data={"csrf": token, "name": "Oops"},
            headers={"Origin": "https://foreign.test"},
        ).status_code
        == 403
    )
    for invalid in (None, "x" * 201, "Bad\x01name"):
        values = {"csrf": token}
        if invalid is not None:
            values["name"] = invalid
        assert client.post(path, data=values).status_code == 400
    if kind == "local":
        assert (
            client.post(path, data={"csrf": token, "name": "  "}).status_code
            == 400
        )
        assert playlists.get(user(service), item_id)["name"] == "Original"
    else:
        assert imports.get(user(service), item_id)["custom_title"] is None


def test_unconfirmed_import_cannot_be_renamed(imported_service):
    service, imports, _, _ = imported_service
    token = browser(service)
    item_id = run(service, imports.preview, user(service), URL)
    assert run(service, imports.refresh_one)
    path = f"/playlist-imports/{item_id}/rename"
    assert (
        f'action="{path}"'
        not in service[0].get(f"/playlist-imports/{item_id}").text
    )
    response = service[0].post(path, data={"csrf": token, "name": "Oops"})
    assert response.status_code == 400
    assert "Confirm the preview first" in response.text
