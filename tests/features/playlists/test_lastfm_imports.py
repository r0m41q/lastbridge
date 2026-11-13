import asyncio
import copy
import time

import httpx
import pytest
from support.catalog import SONG, body, get
from support.imports import browser, due, user
from support.library import run

from lastbridge.features.playlists.adapters.lastfm.imports import (
    LastfmPlaylistProvider,
)
from lastbridge.features.playlists.domain.imports import (
    Snapshot,
    playlist_source,
)
from lastbridge.shared.errors import CatalogError

URL = "https://www.last.fm/user/ignored-user/playlists/12354876"
ROW = {
    "name": SONG["title"],
    "artists": [{"name": SONG["artists"][0]["name"]}],
    "duration": SONG["duration_seconds"],
    "playlinks": [{"affiliate": "youtube", "id": "wrongvideo1"}],
    "session_token": "must-not-persist",
}


@pytest.mark.parametrize(
    "value",
    [
        "12354876",
        " 0012354876 ",
        URL,
        URL + "?utm_source=test#tracks",
        "https://last.fm/de/user/another/playlists/12354876/",
    ],
)
def test_lastfm_inputs_discard_url_owner_and_canonicalize(value):
    assert playlist_source(value, "linked-user") == (
        "lastfm:linked-user:12354876"
    )


@pytest.mark.parametrize(
    "value",
    [
        "-1",
        "١٤٦٢٨٠٧٦",
        "1" * 21,
        "lastfm:other:12354876",
        "http://www.last.fm/user/other/playlists/12354876",
        "https://www.last.fm.evil/user/other/playlists/12354876",
        "https://secret@www.last.fm/user/other/playlists/12354876",
        "https://www.last.fm:8080/user/other/playlists/12354876",
        URL + "/export",
        "https://www.last.fm/user/other/playlists",
        "https://evil.test/user/other/playlists/12354876",
    ],
)
def test_invalid_lastfm_inputs_never_become_fetch_urls(value):
    with pytest.raises(CatalogError):
        playlist_source(value, "linked-user")


@pytest.mark.parametrize("name", [None, "../other", "name?x", "a/b"])
def test_lastfm_import_requires_a_valid_linked_name(name):
    with pytest.raises(CatalogError, match="Link Last.fm"):
        playlist_source("12354876", name)


def test_provider_pagination_matching_duplicates_and_no_video_trust():
    async def scenario():
        calls, searches = [], []

        def respond(request):
            calls.append(request)
            assert request.url.path == (
                "/player/station/user/linked-user/playlists/12354876"
            )
            assert "cookie" not in request.headers
            assert "authorization" not in request.headers
            page = int(request.url.params["page"])
            return httpx.Response(
                200, json={"playlist": [ROW, ROW] if page == 1 else []}
            )

        async def fetch(payload):
            searches.append(payload)
            return {"songs": [SONG]}

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as client:
            provider = LastfmPlaylistProvider(
                client, fetch, lambda row: {"id": row["videoId"]}
            )
            progress = await provider.prepare("lastfm:linked-user:12354876")
            assert len(progress["tracks"]) == 2
            assert "must-not-persist" not in str(progress)
            assert all("videoId" not in t for t in progress["tracks"])
            result = await provider.batch(progress)
            assert [e["song_id"] for e in result["entries"]] == (
                [SONG["videoId"]] * 2
            )
            assert [e["position"] for e in result["entries"]] == [1, 2]
            assert len(searches) == 1
            assert len(calls) == 2
            previous = result | {
                "snapshot": {
                    "tracks": result["tracks"],
                    "entries": result["entries"],
                    "resolver_version": result["resolver_version"],
                }
            }
            unchanged = await provider.prepare(
                "lastfm:linked-user:12354876", previous
            )
            assert unchanged["entries"] == result["entries"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(500),
        httpx.Response(302, headers={"location": "https://evil.test"}),
        httpx.Response(200, text="<title>Client Challenge</title>"),
        httpx.Response(200, json={"error": "missing"}),
        httpx.Response(200, json={"playlist": []}),
        httpx.Response(200, json={"playlist": [None]}),
        httpx.Response(200, json={"playlist": [{"name": "Bad"}]}),
        httpx.Response(200, json={"playlist": [ROW] * 1001}),
        httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)),
    ],
)
def test_provider_failure_never_becomes_an_empty_success(response):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: response)
        ) as client:
            provider = LastfmPlaylistProvider(client, None, None)
            with pytest.raises(CatalogError):
                await provider.prepare("lastfm:linked-user:12354876")

    asyncio.run(scenario())


def test_later_page_failure_and_repeated_pages_reject_partial_snapshot():
    async def scenario():
        for repeating in (False, True):

            def respond(request, repeating=repeating):
                page = int(request.url.params["page"])
                if page > 1 and not repeating:
                    return httpx.Response(503)
                return httpx.Response(200, json={"playlist": [ROW] * 100})

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond)
            ) as client:
                provider = LastfmPlaylistProvider(client, None, None)
                with pytest.raises(CatalogError):
                    await provider.prepare("lastfm:linked-user:12354876")

    asyncio.run(scenario())


def test_lastfm_url_browser_queue_private_refresh_and_account_fence(
    saved_service,
):
    service = saved_service
    client, app, _, _ = service
    imports = app.state.services.playlists.imports
    database = app.state.services.database
    with database.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='linked-user',"
            "link_status='linked' WHERE username='admin'"
        )
    state = {"rows": [ROW, ROW], "fail": False}
    calls = []

    def respond(request):
        calls.append(str(request.url))
        assert "/user/linked-user/playlists/12354876" in request.url.path
        if state["fail"]:
            return httpx.Response(500)
        return httpx.Response(
            200,
            json={
                "playlist": copy.deepcopy(state["rows"])
                if request.url.params["page"] == "1"
                else []
            },
        )

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as website:
            imports.lastfm_provider.client = website
            assert await imports.refresh_one()

    csrf = browser(service)
    assert "Playlist URL or Last.fm ID" in client.get("/playlists").text
    assert (
        client.post("/playlist-imports/preview", data={"url": URL}).status_code
        == 403
    )
    response = client.post(
        "/playlist-imports/preview", data={"csrf": csrf, "url": URL}
    )
    assert response.status_code == 200
    item_id = response.url.path.rsplit("/", 1)[1]
    assert imports.get(user(service), item_id)["source"] == (
        "lastfm:linked-user:12354876"
    )
    assert run(service, imports.preview, user(service), "12354876") == item_id
    assert (
        client.post(
            "/playlist-imports/preview",
            data={"csrf": csrf, "url": URL, "scope": "shared"},
        ).status_code
        == 400
    )
    run(service, scenario)
    item = imports.get(user(service), item_id)
    assert item["state"] == "complete"
    assert len(item["entries"]) == 2
    assert all(e["song_id"] for e in item["entries"])
    assert (
        client.post(
            f"/playlist-imports/{item_id}/confirm", data={"csrf": csrf}
        ).status_code
        == 200
    )
    page = client.get(f"/playlist-imports/{item_id}").text
    assert (
        'href="https://www.last.fm/user/linked-user/playlists/12354876"'
        in page
    )
    assert "Next refresh:" in page
    playlist = body(get(service, "getPlaylist", id=item_id))["playlist"]
    assert playlist["comment"] == "Last.fm import"
    assert playlist["songCount"] == 2
    assert not playlist["public"]
    with pytest.raises(CatalogError):
        imports.get(user(service, "alice"), item_id)

    # Confirmed private Last.fm imports refresh automatically.
    before = imports.get(user(service), item_id)
    state["rows"] = [ROW]
    due(service, item_id)
    run(service, scenario)
    current = imports.get(user(service), item_id)
    assert len(current["entries"]) == 1
    assert current["next_attempt"] > time.time() + 86390
    state["fail"] = True
    due(service, item_id)
    run(service, scenario)
    failed = imports.get(user(service), item_id)
    assert failed["entries"] == current["entries"]
    assert failed["updated"] == current["updated"]
    assert failed["error"]
    assert len(before["entries"]) == 2

    # A relink must not fetch or publish the previous account's source.
    with database.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='changed-user' "
            "WHERE username='admin'"
        )
    run(service, imports.refresh, user(service), item_id)
    due(service, item_id)
    count = len(calls)
    assert run(service, imports.refresh_one)
    failed = imports.get(user(service), item_id)
    assert len(calls) == count
    assert failed["state"] == "failed"
    assert "account changed" in failed["error"]
    assert failed["entries"] == current["entries"]


def test_unlinked_account_cannot_import_lastfm_even_as_admin(saved_service):
    service = saved_service
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name=NULL,link_status='unlinked' "
            "WHERE username='admin'"
        )
    imports = service[1].state.services.playlists.imports
    with pytest.raises(CatalogError, match="Link Last.fm"):
        run(service, imports.preview, user(service), "12354876")


@pytest.mark.parametrize("stage", ["checkpoint", "publish", "confirm"])
def test_relink_during_import_cannot_checkpoint_publish_or_confirm(
    saved_service, stage
):
    service = saved_service
    imports = service[1].state.services.playlists.imports
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='linked-user' WHERE username='admin'"
        )
    owner = user(service)
    item_id = run(service, imports.preview, owner, "12354876")
    job = imports.repository.claim(item_id)
    provider = imports.lastfm_provider
    progress = provider.prepare_data(
        {
            "title": "Last.fm playlist 12354876",
            "tracks": [provider.track(ROW)],
        }
    )
    snapshot = Snapshot(progress["title"], [])
    if stage == "confirm":
        job["progress"] = progress
        imports.repository.publish(job, snapshot)
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='another-account' WHERE id=?",
            (owner["id"],),
        )
    with pytest.raises(CatalogError, match="account changed"):
        if stage == "checkpoint":
            imports.repository.checkpoint(job, progress)
        elif stage == "publish":
            job["progress"] = progress
            imports.repository.publish(job, snapshot)
        else:
            imports.confirm(owner, item_id)
    assert not imports.get(owner, item_id)["ready"]


def test_lastfm_pause_restart_skip_and_refresh_survive(saved_service):
    service = saved_service
    imports = service[1].state.services.playlists.imports
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='linked-user' WHERE username='admin'"
        )
    owner = user(service)
    source = f"lastfm:{owner['lastfm_name']}:12354876"
    item_id = run(service, imports.preview, owner, "12354876")
    imports.control(owner, item_id, "pause")
    assert not run(service, imports.refresh_one)
    imports.repository.recover()
    assert imports.get(owner, item_id)["state"] == "paused"
    imports.control(owner, item_id, "resume")

    async def process():
        def respond(request):
            return httpx.Response(
                200,
                json={
                    "playlist": [ROW]
                    if request.url.params["page"] == "1"
                    else []
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as website:
            imports.lastfm_provider.client = website
            assert await imports.refresh_one()

    run(service, process)
    item = imports.get(owner, item_id)
    assert item["source"] == source
    imports.confirm(owner, item_id)
    key = item["entries"][0]["source_key"]
    imports.decide(owner, item_id, key, "", action="skip")
    assert imports.get(owner, item_id)["entries"][0]["skipped"]
    run(service, imports.refresh, owner, item_id)
    run(service, process)
    imports.repository.recover()
    refreshed = imports.get(owner, item_id)
    assert refreshed["entries"][0]["skipped"]
    assert refreshed["entries"][0]["song_id"] is None
    imports.decide(owner, item_id, key, "", action="restore")
    assert imports.get(owner, item_id)["entries"][0]["song_id"]
    # A failed refresh lease resumes with the same provider and source.
    due(service, item_id)
    job = imports.repository.claim()
    assert job["source"] == source
    imports.repository.recover()
    run(service, process)
    assert imports.get(owner, item_id)["state"] == "complete"
