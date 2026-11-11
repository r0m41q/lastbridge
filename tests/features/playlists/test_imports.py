import asyncio
import time
from xml.etree.ElementTree import fromstring

import pytest
from support.catalog import SONG, body, get
from support.imports import URL, accept, due, user
from support.library import run

from lastbridge.features.playlists.domain.imports import DAY, playlist_source
from lastbridge.shared.errors import CatalogError


@pytest.mark.parametrize(
    "url",
    [
        "http://music.youtube.com/playlist?list=PL_test_playlist123",
        "https://evil.test/playlist?list=PL_test_playlist123",
        "https://music.youtube.com.evil.test/playlist"
        "?list=PL_test_playlist123",
        "https://user:password@music.youtube.com/playlist"
        "?list=PL_test_playlist123",
        "https://music.youtube.com:8080/playlist?list=PL_test_playlist123",
        "https://music.youtube.com/watch?list=PL_test_playlist123",
        URL + "&list=PL_other_playlist123",
        URL + "#fragment",
        "https://music.youtube.com/playlist?list=../private",
        "not a URL",
    ],
)
def test_import_url_rejects_arbitrary_hosts_and_malformed_lists(url):
    with pytest.raises(CatalogError):
        playlist_source(url)


def test_import_preview_order_duplicates_report_and_metadata_only(
    imported_service,
):
    service, imports, _, calls = imported_service
    owner = user(service)
    item_id = run(service, imports.preview, owner, URL)
    assert run(service, imports.refresh_one)
    preview = imports.get(owner, item_id)
    assert len(preview["entries"]) == 4
    assert [e["position"] for e in preview["entries"]] == [1, 2, 3, 4]
    assert preview["entries"][1]["reason"] == "Unavailable"
    assert not service[1].state.services.playlists.imported(owner)
    assert body(get(service, "getPlaylist", id=item_id))["error"]["code"] == 70
    with pytest.raises(CatalogError):
        imports.get(user(service, "alice"), item_id)
    assert imports.confirm(owner, item_id) == item_id
    result = body(get(service, "getPlaylist", id=item_id))["playlist"]
    assert result["songCount"] == 3
    ids = [e["id"] for e in result["entry"]]
    assert ids[0] == ids[2] and ids[0] != ids[1]
    assert result["readonly"] and not result["public"]
    xml = fromstring(get(service, "getPlaylist", id=item_id, f="xml").content)
    assert len(xml.find("{*}playlist").findall("{*}entry")) == 3
    assert len(calls) == 1
    database = service[1].state.services.database
    with database.connection() as connection:
        for table in ("save_jobs", "favorites", "playlist_entries"):
            assert (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[
                    0
                ]
                == 0
            )
    with pytest.raises(CatalogError):
        imports.confirm(owner, item_id)


def test_shared_visibility_single_refresh_private_isolation_and_copy(
    imported_service,
):
    service, imports, state, calls = imported_service
    chart = accept(imported_service, shared=True)
    owner = user(service)
    alice = user(service, "alice")
    with pytest.raises(CatalogError, match="Administrator"):
        run(service, imports.preview, alice, URL, True)
    private = accept(imported_service, name="alice")
    with pytest.raises(CatalogError):
        imports.get(owner, private)
    for method in ("refresh", "remove"):
        with pytest.raises(CatalogError, match="Administrator"):
            if method == "refresh":
                run(service, imports.refresh, alice, chart)
            else:
                imports.remove(alice, chart)
    for who, expected in (("admin", {chart}), ("alice", {chart, private})):
        lists = body(get(service, "getPlaylists", apiKey=service[3][who]))
        ids = {
            p["id"]
            for p in lists["playlists"]["playlist"]
            if p["id"].startswith("y")
        }
        assert ids == expected
    before = body(get(service, "getPlaylist", id=chart))["playlist"]
    playlists = service[1].state.services.playlists
    copied = run(service, playlists.copy, alice, chart, "My copy")
    state["tracks"], state["trackCount"] = [SONG], 1
    due(service, chart)
    due(service, private)
    count = len(calls)
    assert run(service, imports.refresh_one)
    assert not run(service, imports.refresh_one)
    assert len(calls) == count + 1
    assert imports.get(owner, chart)["next_attempt"] > time.time() + DAY - 5
    assert (
        body(get(service, "getPlaylist", id=chart))["playlist"]["songCount"]
        == 1
    )
    assert before["songCount"] == 3
    assert playlists.get(alice, copied["id"])["songCount"] == 3
    assert (
        imports.get(alice, private)["entries"]
        != imports.get(owner, chart)["entries"]
    )
    for method, params in (
        ("updatePlaylist", {"playlistId": chart, "name": "Bad"}),
        ("deletePlaylist", {"id": chart}),
        ("createPlaylist", {"playlistId": chart}),
    ):
        assert body(get(service, method, **params))["error"]["code"] == 50


def test_failure_preserves_snapshot_manual_resets_clock_and_restart_recovers(
    imported_service,
):
    service, imports, state, _ = imported_service
    item_id = accept(imported_service, shared=True)
    owner = user(service)
    before = imports.get(owner, item_id)
    state["fail"] = True
    due(service, item_id)
    assert run(service, imports.refresh_one)
    failed = imports.get(owner, item_id)
    assert failed["entries"] == before["entries"]
    assert failed["updated"] == before["updated"]
    assert failed["error"] and failed["next_attempt"] > time.time() + 55
    assert not run(service, imports.refresh_one)
    state["fail"] = False
    due(service, item_id)
    run(service, imports.refresh, owner, item_id)
    assert run(service, imports.refresh_one)
    refreshed = imports.get(owner, item_id)
    assert refreshed["error"] is None
    assert refreshed["next_attempt"] > time.time() + DAY - 5
    job = imports.repository.claim(item_id)
    assert job
    imports.repository.recover()
    recovered = imports.get(owner, item_id)
    assert recovered["token"] is None
    assert recovered["state"] == "queued"
    imports.remove(owner, item_id)
    assert not imports.rows(owner)


@pytest.mark.parametrize(
    "change",
    [
        {"tracks": [SONG] * 1001, "trackCount": 1001},
        {"tracks": [SONG], "trackCount": 4},
        {"tracks": None},
        {"title": None},
    ],
)
def test_partial_or_oversized_payload_cannot_replace_snapshot(
    imported_service,
    change,
):
    service, imports, state, _ = imported_service
    item_id = accept(imported_service, shared=True)
    owner = user(service)
    before = imports.get(owner, item_id)
    state.update(change)
    run(service, imports.refresh, owner, item_id)
    assert run(service, imports.refresh_one)
    assert imports.get(owner, item_id)["entries"] == before["entries"]


def test_same_source_is_deduplicated_and_private_sources_stay_separate(
    imported_service,
):
    service, imports, _, _ = imported_service
    first = accept(imported_service, shared=True)
    assert accept(imported_service, shared=True) == first
    mine = accept(imported_service)
    theirs = accept(imported_service, name="alice")
    assert len({first, mine, theirs}) == 3
    assert len(imports.rows(user(service))) == 2


def test_cancelled_refresh_keeps_snapshot_and_cannot_resurrect_deleted_chart(
    imported_service,
):
    service, imports, _, _ = imported_service
    item_id = accept(imported_service, shared=True)
    owner = user(service)
    before = imports.get(owner, item_id)

    async def scenario():
        entered = asyncio.Event()
        original = imports.provider.load
        prepare = imports.provider.prepare

        async def waiting(source, previous):
            entered.set()
            await asyncio.Event().wait()

        imports.provider.prepare = waiting
        await imports.refresh(owner, item_id)
        task = asyncio.create_task(imports.refresh_one())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert imports.get(owner, item_id)["entries"] == before["entries"]
        assert imports.get(owner, item_id)["token"] is None
        due(service, item_id)
        job = imports.repository.claim(item_id)
        imports.remove(owner, item_id)
        imports.provider.prepare = prepare
        snapshot = await original("PL_test_playlist123")
        imports.repository.publish(job, snapshot)
        assert not imports.rows(owner)

    run(service, scenario)


def test_previews_expire_and_private_removal_is_owner_only(imported_service):
    service, imports, _, _ = imported_service
    owner, alice = user(service), user(service, "alice")
    preview = run(service, imports.preview, owner, URL)
    assert run(service, imports.refresh_one)
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE playlist_imports SET updated=0 WHERE id=?", (preview,)
        )
    with pytest.raises(CatalogError):
        imports.confirm(owner, preview)
    with pytest.raises(CatalogError):
        imports.get(owner, preview)
    item_id = accept(imported_service, name="alice")
    with pytest.raises(CatalogError):
        imports.remove(owner, item_id)
    imports.remove(alice, item_id)
    assert not imports.rows(alice)


def test_shared_refresh_has_no_dependency_on_user_activity(imported_service):
    service, imports, _, calls = imported_service
    item_id = accept(imported_service, shared=True)
    with service[1].state.services.database.transaction() as connection:
        connection.execute("DELETE FROM mix_activity")
    due(service, item_id)
    assert run(service, imports.refresh_one)
    assert len(calls) == 2
    owner = user(service)
    assert imports.get(owner, item_id)["token"] is None


def test_chart_custom_name_survives_refresh_and_can_be_reset(imported_service):
    service, imports, state, _ = imported_service
    item_id = accept(imported_service, shared=True)
    owner = user(service)
    before = imports.get(owner, item_id)
    job = imports.repository.claim(item_id)
    leased = imports.get(owner, item_id)
    imports.rename_shared(owner, item_id, "  My chart <&>  ")
    renamed = imports.get(owner, item_id)
    assert renamed["title"] == "My chart <&>"
    assert renamed["custom_title"] == "My chart <&>"
    assert renamed["next_attempt"] == leased["next_attempt"]
    assert renamed["token"] == job["token"]
    assert renamed["source"] == before["source"]
    assert renamed["entries"] == before["entries"]
    state["title"] = "New YouTube title"
    state["tracks"], state["trackCount"] = [SONG], 1
    snapshot = run(service, imports.provider.load, job["source"])
    imports.repository.publish(job, snapshot)
    assert imports.get(owner, item_id)["title"] == "My chart <&>"
    for account in ("admin", "alice"):
        response = get(
            service, "getPlaylist", id=item_id, apiKey=service[3][account]
        )
        playlist = body(response)["playlist"]
        assert playlist["name"] == "My chart <&>"
        assert playlist["songCount"] == 1
        lists = body(get(service, "getPlaylists", apiKey=service[3][account]))[
            "playlists"
        ]["playlist"]
        assert next(p for p in lists if p["id"] == item_id)["name"] == (
            "My chart <&>"
        )
    xml = fromstring(get(service, "getPlaylist", id=item_id, f="xml").content)
    assert xml.find("{*}playlist").get("name") == "My chart <&>"
    imports.rename_shared(owner, item_id, "")
    assert imports.get(owner, item_id)["title"] == "New YouTube title"
    assert imports.get(owner, item_id)["custom_title"] is None
    state["title"] = "Later YouTube title"
    run(service, imports.refresh, owner, item_id)
    assert run(service, imports.refresh_one)
    assert imports.get(owner, item_id)["title"] == "Later YouTube title"


def test_chart_rename_role_validation_and_private_isolation(imported_service):
    service, imports, _, _ = imported_service
    owner = user(service)
    chart = accept(imported_service, shared=True)
    private = accept(imported_service)
    draft = run(service, imports.preview, owner, URL, True)
    with pytest.raises(CatalogError, match="Administrator"):
        imports.rename_shared(user(service, "alice"), chart, "Not allowed")
    for item_id in (private, draft, "missing"):
        with pytest.raises(CatalogError, match="Shared chart not found"):
            imports.rename_shared(owner, item_id, "Not a chart")
    for name in (None, "x" * 201, "Bad\x00title"):
        with pytest.raises(CatalogError, match="Invalid parameter"):
            imports.rename_shared(owner, chart, name)
    assert imports.get(owner, chart)["custom_title"] is None
