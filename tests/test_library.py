import asyncio
import shutil
from types import SimpleNamespace
from urllib.parse import urlencode
from xml.etree.ElementTree import fromstring

import pytest
import test_catalog
from test_accounts import csrf
from test_audio import AUDIO, OTHER, SOURCE, THIRD, FakeAudio
from test_catalog import SONG, body, get, search

from lastbridge.accounts import Accounts
from lastbridge.audio import AudioCache
from lastbridge.catalog import Catalog, CatalogError, identity
from lastbridge.library import Library
from lastbridge.web import COOKIE

catalog_service = test_catalog.catalog_service


@pytest.fixture
def saved_service(catalog_service):
    client, app, _, _ = catalog_service
    # Explicitly drive the durable worker in most tests, avoiding scheduling
    # races while still exercising real HTTP mutations, SQLite and filesystem.
    client.portal.call(app.state.library.close)
    app.state.audio.provider = FakeAudio()
    return catalog_service


def run(service, function, *args):
    return service[0].portal.call(function, *args)


def user_id(service, name="admin"):
    return service[1].state.accounts.user(name)["id"]


def complete_jobs(service):
    library = service[1].state.library
    while run(service, library.process_one):
        pass


def test_song_heart_is_immediate_private_and_idempotent(saved_service):
    _, app, _, keys = saved_service
    song = search(saved_service)["song"][0]
    assert body(get(saved_service, "star", id=song["id"]))["status"] == "ok"
    assert not app.state.audio.provider.calls
    starred = body(get(saved_service, "getStarred2"))["starred2"]["song"]
    assert len(starred) == 1 and starred[0]["starred"].endswith("Z")
    original_time = starred[0]["starred"]
    assert body(get(saved_service, "star", id=song["id"]))["status"] == "ok"
    assert (
        app.state.library.downloads(user_id(saved_service))[0]["state"]
        == "pending"
    )
    assert (
        body(get(saved_service, "getSong", id=song["id"]))["song"]["starred"]
        == original_time
    )
    assert (
        body(get(saved_service, "search3", query="Daft Punk"))[
            "searchResult3"
        ]["song"][0]["starred"]
        == original_time
    )
    assert "starred" not in app.state.catalog.read(song["id"])["document"]
    assert (
        body(get(saved_service, "getStarred2", apiKey=keys["alice"]))[
            "starred2"
        ]["song"]
        == []
    )
    assert (
        "starred"
        not in body(
            get(saved_service, "getSong", id=song["id"], apiKey=keys["alice"])
        )["song"]
    )
    complete_jobs(saved_service)
    entry = app.state.audio.entries[f"{SOURCE}-aac"]
    assert entry["saved"]
    assert app.state.audio.path(f"{SOURCE}-aac", entry).read_bytes() == AUDIO
    assert not list(app.state.audio.directory.glob("*.m4a"))
    assert (
        app.state.library.downloads(user_id(saved_service))[0]["state"]
        == "complete"
    )
    assert get(saved_service, "stream", id=song["id"]).content == AUDIO
    assert app.state.audio.provider.calls == [SOURCE]


def test_album_and_song_pins_overlap_across_users(saved_service):
    _, app, _, keys = saved_service
    result = search(saved_service)
    song, album = result["song"][0]["id"], result["album"][0]["id"]
    for params in (
        {"albumId": album},
        {"id": song},
        {"id": song, "apiKey": keys["alice"]},
    ):
        assert body(get(saved_service, "star", **params))["status"] == "ok"
    complete_jobs(saved_service)
    assert len(app.state.audio.provider.calls) == 2
    assert (
        len(app.state.library.collection(user_id(saved_service))["song"]) == 2
    )
    assert (
        len(
            app.state.library.collection(user_id(saved_service, "alice"))[
                "song"
            ]
        )
        == 1
    )
    assert (
        len(body(get(saved_service, "getStarred2"))["starred2"]["song"]) == 1
    )
    get(saved_service, "unstar", albumId=album)
    assert (
        len(app.state.library.collection(user_id(saved_service))["song"]) == 1
    )
    assert app.state.audio.entries[f"{SOURCE}-aac"]["saved"]
    other = identity("song", "othertrack1")
    assert not app.state.audio.entries["othertrack1-aac"].get("saved")
    # Another user's pin survives this user's last heart removal.
    get(saved_service, "unstar", id=song)
    assert app.state.library.collection(user_id(saved_service))["song"] == []
    assert app.state.audio.entries[f"{SOURCE}-aac"]["saved"]
    assert get(saved_service, "download", id=song).content == AUDIO
    get(saved_service, "unstar", id=song, apiKey=keys["alice"])
    assert not app.state.audio.entries[f"{SOURCE}-aac"].get("saved")
    assert app.state.catalog.read(other)["source"] == "othertrack1"
    assert app.state.catalog.read(song)["source"] == SOURCE


def test_artist_heart_is_metadata_only_and_xml_starred(saved_service):
    _, app, provider, _ = saved_service
    result = search(saved_service)
    artist = result["artist"][0]["id"]
    calls = len(provider.calls)
    assert body(get(saved_service, "star", artistId=artist))["status"] == "ok"
    assert len(provider.calls) == calls
    assert not app.state.library.downloads(user_id(saved_service))
    assert not app.state.audio.pins
    response = get(saved_service, "getStarred.view", f="xml")
    root = fromstring(response.content)
    element = root.find("{*}starred/{*}artist")
    assert element.attrib["id"] == artist and element.attrib[
        "starred"
    ].endswith("Z")
    assert (
        body(get(saved_service, "getArtists"))["artists"]["index"][0][
            "artist"
        ][0]["id"]
        == artist
    )


def test_saved_library_search_paging_browsing_and_no_provider_calls(
    saved_service,
):
    _, app, provider, keys = saved_service
    result = search(saved_service)
    song, album = result["song"][0], result["album"][0]
    get(saved_service, "star", id=song["id"])
    # Extra public metadata must not leak into library enumeration.
    app.state.catalog.song(SONG | {"videoId": OTHER, "title": "Not saved"})
    calls = len(provider.calls)
    for endpoint, field in (
        ("search3", "searchResult3"),
        ("search2", "searchResult2"),
    ):
        saved = body(get(saved_service, endpoint, query="", songCount=500))[
            field
        ]
        assert [s["id"] for s in saved["song"]] == [song["id"]]
        assert len(saved["album"]) == len(saved["artist"]) == 1
        assert saved["album"][0]["songCount"] == 1
        assert (
            body(get(saved_service, endpoint, query='""', songOffset=1))[
                field
            ]["song"]
            == []
        )
        assert (
            body(get(saved_service, endpoint, query="", songCount=0))[field][
                "song"
            ]
            == []
        )
    albums = body(get(saved_service, "getAlbumList2", type="newest"))[
        "albumList2"
    ]["album"]
    assert albums[0]["id"] == album["id"]
    assert (
        body(get(saved_service, "getAlbumList2", type="starred"))[
            "albumList2"
        ]["album"]
        == []
    )
    assert body(get(saved_service, "getMusicDirectory", id="1"))["directory"][
        "child"
    ]
    assert (
        body(get(saved_service, "getScanStatus"))["scanStatus"]["count"] == 1
    )
    assert body(get(saved_service, "search3", query="", apiKey=keys["alice"]))[
        "searchResult3"
    ] == {"artist": [], "album": [], "song": []}
    assert len(provider.calls) == calls


def test_batch_get_and_post_validation_is_atomic(saved_service):
    client, app, _, keys = saved_service
    result = search(saved_service)
    song = result["song"][0]["id"]
    other = app.state.catalog.song(SONG | {"videoId": OTHER})["id"]
    assert (
        body(get(saved_service, "star", id=[song, "missing"]))["error"]["code"]
        == 70
    )
    assert not app.state.library.stars(user_id(saved_service))
    assert (
        body(get(saved_service, "star", albumId=song))["error"]["code"] == 70
    )
    assert body(get(saved_service, "star"))["error"]["code"] == 10
    params = {
        "apiKey": keys["admin"],
        "v": "1.16.1",
        "c": "test",
        "f": "json",
        "id": [song, other],
    }
    response = client.post(
        "/rest/star.view",
        content=urlencode(params, doseq=True),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert body(response)["status"] == "ok"
    assert len(app.state.library.stars(user_id(saved_service))) == 2
    assert (
        body(get(saved_service, "unstar", id=[song, other]))["status"] == "ok"
    )
    assert not app.state.library.stars(user_id(saved_service))
    assert (
        fromstring(
            client.get(
                "/rest/star",
                params=list(params.items())[:-1]
                + [("apiKey", "other"), ("id", song)],
            ).content
        )
        .find("{*}error")
        .attrib["code"]
        == "10"
    )
    assert (
        fromstring(get(saved_service, "star", id=[song] * 129).content)
        .find("{*}error")
        .attrib["code"]
        == "10"
    )
    assert (
        body(client.get("/rest/star", params={"f": "json", "id": song}))[
            "error"
        ]["code"]
        == 40
    )
    assert (
        body(get(saved_service, "star", id=song, apiKey=keys["unlinked"]))[
            "error"
        ]["code"]
        == 50
    )


def test_album_resolution_failure_leaves_no_half_saved_batch(saved_service):
    _, app, provider, _ = saved_service
    result = search(saved_service)
    provider.fail = True
    response = get(
        saved_service,
        "star",
        id=result["song"][0]["id"],
        albumId=result["album"][0]["id"],
    )
    assert body(response)["status"] == "failed"
    assert not app.state.library.stars(user_id(saved_service))
    assert not app.state.audio.pins


def test_failure_backoff_manual_retry_and_private_browser(saved_service):
    client, app, _, _ = saved_service
    song = search(saved_service)["song"][0]["id"]
    app.state.audio.provider.fail = True
    get(saved_service, "star", id=song)
    library = app.state.library
    for attempt in range(3):
        assert run(saved_service, library.process_one)
        row = library.downloads(user_id(saved_service))[0]
        assert row["attempts"] == attempt + 1
        if attempt < 2:
            assert row["state"] == "pending"
            assert not run(saved_service, library.process_one)  # backoff
            with app.state.accounts.transaction() as connection:
                connection.execute("UPDATE save_jobs SET next_attempt=0")
    assert row["state"] == "failed"
    assert body(get(saved_service, "getStarred2"))["starred2"]["song"]
    assert client.get("/library", follow_redirects=False).status_code == 303
    # Browser session is independent of the linked client's secret.
    token = app.state.accounts.login(
        "admin", "a sufficiently long password", "fixture"
    )
    client.cookies.set(COOKIE, token)
    page = client.get("/library")
    assert "Download failed" in page.text and "&lt;studio&gt;" in page.text
    assert (
        client.post("/library/retry", data={"csrf": "wrong"}).status_code
        == 403
    )
    assert (
        client.post(
            "/library/retry", data={"csrf": csrf(page)}, follow_redirects=False
        ).status_code
        == 303
    )
    app.state.audio.provider.fail = False
    complete_jobs(saved_service)
    assert library.downloads(user_id(saved_service))[0]["state"] == "complete"
    token = app.state.accounts.login(
        "alice", "a sufficiently long password", "fixture"
    )
    client.cookies.set(COOKIE, token)
    assert "Instant Crush" not in client.get("/library").text
    assert not library.downloads(user_id(saved_service, "alice"))


def make_store(tmp_path):
    accounts = Accounts(tmp_path)
    for name in ("admin", "alice"):
        accounts.create_user(name, "a sufficiently long password")
    catalog = Catalog(accounts)
    song = catalog.song(SONG)
    library = Library(accounts, catalog, "aac")
    audio = AudioCache(
        tmp_path / "cache" / "audio",
        saved_directory=tmp_path / "saved" / "audio",
        budget=32768,
        maximum=8192,
        reserve=0,
        provider=FakeAudio(),
        pins=library.pins(),
    )
    library.audio = audio
    return accounts, catalog, library, audio, song


def test_restart_preserves_intent_and_reuses_completed_files(tmp_path):
    async def scenario():
        accounts, _, library, audio, song = make_store(tmp_path)
        uid = accounts.user("admin")["id"]
        await library.change(uid, [(song["id"], None)], True)
        # Simulate interruption after the durable job claim.
        with accounts.transaction() as connection:
            connection.execute(
                "UPDATE save_jobs SET state='running',attempts=1"
            )
        await audio.close()
        library = Library(accounts, Catalog(accounts), "aac")
        provider = FakeAudio()
        audio = AudioCache(
            tmp_path / "cache" / "audio",
            saved_directory=tmp_path / "saved" / "audio",
            budget=32768,
            maximum=8192,
            reserve=0,
            provider=provider,
            pins=library.pins(),
        )
        library.audio = audio
        await library.reconcile(startup=True)
        assert library.downloads(uid)[0]["state"] == "pending"
        await library.process_one()
        assert provider.calls == [SOURCE]
        await audio.close()
        # Completed retained files survive an arbitrarily small cache budget.
        audio = AudioCache(
            tmp_path / "cache" / "audio",
            saved_directory=tmp_path / "saved" / "audio",
            budget=1024,
            maximum=8192,
            reserve=0,
            provider=provider,
            pins=library.pins(),
        )
        library.audio = audio
        await library.reconcile(startup=True)
        key, entry = await audio.acquire(SOURCE)
        assert entry["saved"] and entry["size"] == len(AUDIO)
        await audio.release(key)
        assert not await library.process_one()
        assert provider.calls == [SOURCE]
        await audio.close()

    asyncio.run(scenario())


def test_shared_inflight_save_does_not_duplicate_playback(tmp_path):
    async def scenario():
        accounts, _, library, audio, song = make_store(tmp_path)
        began, finish = asyncio.Event(), asyncio.Event()

        class Blocking(FakeAudio):
            async def fetch(self, *args):
                began.set()
                await finish.wait()
                return await super().fetch(*args)

        provider = audio.provider = Blocking()
        response_task = asyncio.create_task(audio.response(SOURCE))
        await began.wait()
        await library.change(
            accounts.user("admin")["id"], [(song["id"], None)], True
        )
        job = asyncio.create_task(library.process_one())
        await asyncio.sleep(0)
        finish.set()
        response = await response_task
        await job
        assert provider.calls == [SOURCE]
        # The response was constructed before promotion. Its old hard-link path
        # must remain valid until ASGI completes, even after an unlike.
        assert response.path.read_bytes() == AUDIO
        await library.change(
            accounts.user("admin")["id"], [(song["id"], None)], False
        )
        assert response.path.read_bytes() == AUDIO
        await audio.release(response.key)
        assert not audio.entries[response.key].get("saved")
        assert not list(audio.saved_directory.glob("*.m4a"))
        await audio.close()

    asyncio.run(scenario())


def test_retention_is_outside_cache_budget_and_pins_prevent_eviction(tmp_path):
    async def scenario():
        accounts, _, library, audio, song = make_store(tmp_path)
        await library.change(
            accounts.user("admin")["id"], [(song["id"], None)], True
        )
        await library.process_one()
        saved = audio.path(f"{SOURCE}-aac", audio.entries[f"{SOURCE}-aac"])
        for source in (OTHER, THIRD, "othertrack1", OTHER):
            key, _ = await audio.acquire(source)
            await audio.release(key)
        assert saved.read_bytes() == AUDIO
        assert audio.entries[f"{SOURCE}-aac"]["saved"]
        await audio.update_pins(set())
        assert not audio.entries[f"{SOURCE}-aac"].get("saved")
        await audio.close()

    asyncio.run(scenario())


def test_disk_full_preserves_favorite_existing_saved_audio_and_retry(
    tmp_path, monkeypatch
):
    async def scenario():
        accounts, catalog, library, audio, song = make_store(tmp_path)
        uid = accounts.user("admin")["id"]
        await library.change(uid, [(song["id"], None)], True)
        await library.process_one()
        other = catalog.song(SONG | {"videoId": OTHER})
        await library.change(uid, [(other["id"], None)], True)
        real_usage = shutil.disk_usage
        monkeypatch.setattr(
            "lastbridge.audio.shutil.disk_usage",
            lambda path: SimpleNamespace(free=0),
        )
        await library.process_one()
        assert len(library.stars(uid)) == 2
        assert library.downloads(uid)[0]["state"] in ("pending", "complete")
        key, _ = await audio.acquire(SOURCE)
        await audio.release(key)
        assert audio.provider.calls == [SOURCE]
        monkeypatch.setattr("lastbridge.audio.shutil.disk_usage", real_usage)
        library.retry(uid)
        await library.process_one()
        assert all(r["state"] == "complete" for r in library.downloads(uid))
        await audio.close()

    asyncio.run(scenario())


def test_cancelled_worker_recovers_and_unlike_during_download_does_not_pin(
    tmp_path,
):
    async def scenario():
        accounts, _, library, audio, song = make_store(tmp_path)
        began, finish = asyncio.Event(), asyncio.Event()

        class Blocking(FakeAudio):
            async def fetch(self, *args):
                began.set()
                await finish.wait()
                return await super().fetch(*args)

        audio.provider = Blocking()
        uid = accounts.user("admin")["id"]
        await library.change(uid, [(song["id"], None)], True)
        await library.start(audio)
        await began.wait()
        await library.change(uid, [(song["id"], None)], False)
        finish.set()
        # The worker still owns its lease until that shared acquisition ends.
        await asyncio.gather(*list(audio.jobs.values()))
        for _ in range(10):
            if not audio.leases:
                break
            await asyncio.sleep(0)
        assert not library.downloads(uid)
        assert not audio.entries[f"{SOURCE}-aac"].get("saved")
        await library.close()
        await audio.close()

    asyncio.run(scenario())


def test_v2_migration_preserves_catalog_accounts_and_credentials(tmp_path):
    accounts, catalog, _, audio, song = make_store(tmp_path)
    asyncio.run(audio.close())
    secret = accounts.rotate_client_secret(accounts.user("admin")["id"])
    with accounts.transaction() as connection:
        connection.execute("DROP TABLE favorite_tracks")
        connection.execute("DROP TABLE favorites")
        connection.execute("DROP TABLE save_jobs")
        connection.execute("ALTER TABLE users DROP COLUMN library_changed")
        connection.execute("PRAGMA user_version=2")
    migrated = Accounts(tmp_path)
    assert migrated.client_user({"apiKey": secret})["username"] == "admin"
    assert Catalog(migrated).read(song["id"]) == catalog.read(song["id"])
    with migrated.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert (
            connection.execute("SELECT count(*) FROM favorites").fetchone()[0]
            == 0
        )


def test_promotion_failure_keeps_favorite_and_reuses_cache_on_retry(
    tmp_path, monkeypatch
):
    async def scenario():
        accounts, _, library, audio, song = make_store(tmp_path)
        uid = accounts.user("admin")["id"]
        await library.change(uid, [(song["id"], None)], True)
        original = audio.manifest

        def fail_saved(key, entry):
            if entry.get("saved"):
                raise OSError("disk full")
            original(key, entry)

        monkeypatch.setattr(audio, "manifest", fail_saved)
        await library.process_one()
        assert library.stars(uid)
        assert library.downloads(uid)[0]["state"] == "pending"
        assert not audio.entries[f"{SOURCE}-aac"].get("saved")
        assert audio.provider.calls == [SOURCE]
        monkeypatch.setattr(audio, "manifest", original)
        library.retry(uid)
        await library.process_one()
        assert audio.entries[f"{SOURCE}-aac"]["saved"]
        assert audio.provider.calls == [SOURCE]
        await audio.close()

    asyncio.run(scenario())


def test_completed_playback_survives_failed_lru_metadata_write(
    tmp_path, monkeypatch
):
    async def scenario():
        _, _, _, audio, _ = make_store(tmp_path)
        key, _ = await audio.acquire(SOURCE)
        await audio.release(key)

        def full(*args):
            raise OSError("disk full")

        monkeypatch.setattr(audio, "manifest", full)
        key, entry = await audio.acquire(SOURCE)
        assert audio.path(key, entry).read_bytes() == AUDIO
        await audio.release(key)
        assert audio.provider.calls == [SOURCE]
        await audio.close()

    asyncio.run(scenario())


def test_failed_jobs_survive_restart_and_retry_cannot_change_other_users_jobs(
    tmp_path,
):
    async def scenario():
        accounts, catalog, library, audio, song = make_store(tmp_path)
        admin, alice = (
            accounts.user(name)["id"] for name in ("admin", "alice")
        )
        other = catalog.song(SONG | {"videoId": OTHER})
        await library.change(admin, [(song["id"], None)], True)
        await library.change(alice, [(other["id"], None)], True)
        with accounts.transaction() as connection:
            connection.execute(
                "UPDATE save_jobs SET state='failed',attempts=3"
            )
        await library.reconcile(startup=True)
        assert library.downloads(admin)[0]["state"] == "failed"
        library.retry(admin)
        assert library.downloads(admin)[0]["state"] == "pending"
        assert library.downloads(alice)[0]["state"] == "failed"
        assert library.downloads(alice)[0]["attempts"] == 3
        await audio.close()

    asyncio.run(scenario())


def test_missing_saved_file_is_requeued_but_corrupt_saved_file_is_preserved(
    tmp_path,
):
    async def scenario():
        accounts, _, library, audio, song = make_store(tmp_path)
        uid = accounts.user("admin")["id"]
        await library.change(uid, [(song["id"], None)], True)
        await library.process_one()
        key = f"{SOURCE}-aac"
        path = audio.path(key, audio.entries[key])
        await audio.close()
        path.unlink()
        audio = AudioCache(
            tmp_path / "cache" / "audio",
            saved_directory=tmp_path / "saved" / "audio",
            budget=32768,
            maximum=8192,
            reserve=0,
            provider=FakeAudio(),
            pins=library.pins(),
        )
        library.audio = audio
        await library.reconcile(startup=True)
        assert library.downloads(uid)[0]["state"] == "pending"
        await library.process_one()
        assert path.read_bytes() == AUDIO
        await audio.close()
        path.write_bytes(b"damaged")
        with pytest.raises(RuntimeError, match="needs repair"):
            AudioCache(
                tmp_path / "cache" / "audio",
                saved_directory=tmp_path / "saved" / "audio",
                budget=32768,
                maximum=8192,
                reserve=0,
                pins=library.pins(),
            )
        assert path.read_bytes() == b"damaged"

    asyncio.run(scenario())


def test_shutdown_requeues_running_save_after_restart(tmp_path):
    async def scenario():
        accounts, _, library, audio, song = make_store(tmp_path)
        began = asyncio.Event()

        class Blocking(FakeAudio):
            async def fetch(self, source, policy, directory, *limits):
                (directory / "audio.m4a.part").write_bytes(b"partial")
                began.set()
                await asyncio.Event().wait()

        audio.provider = Blocking()
        uid = accounts.user("admin")["id"]
        await library.change(uid, [(song["id"], None)], True)
        await library.start(audio)
        await began.wait()
        await library.close()
        await audio.close()
        assert not audio.jobs and not audio.leases and audio.reserved == 0
        assert library.downloads(uid)[0]["state"] == "running"
        audio = AudioCache(
            tmp_path / "cache" / "audio",
            saved_directory=tmp_path / "saved" / "audio",
            budget=32768,
            maximum=8192,
            reserve=0,
            provider=FakeAudio(),
            pins=library.pins(),
        )
        library.audio = audio
        await library.reconcile(startup=True)
        assert library.downloads(uid)[0]["state"] == "pending"
        await library.process_one()
        assert library.downloads(uid)[0]["state"] == "complete"
        await audio.close()

    asyncio.run(scenario())


def test_background_worker_runs_from_http_heart_without_manual_drive(
    catalog_service,
):
    async def wait_saved(library, uid):
        async with asyncio.timeout(3):
            while (
                not library.downloads(uid)
                or library.downloads(uid)[0]["state"] != "complete"
            ):
                await asyncio.sleep(0.01)

    _, app, _, _ = catalog_service
    app.state.audio.provider = FakeAudio()
    song = search(catalog_service)["song"][0]["id"]
    assert body(get(catalog_service, "star", id=song))["status"] == "ok"
    run(
        catalog_service,
        wait_saved,
        app.state.library,
        user_id(catalog_service),
    )
    assert app.state.audio.entries[f"{SOURCE}-aac"]["saved"]
    assert app.state.audio.provider.calls == [SOURCE]


def test_saved_sync_can_page_past_the_remote_search_cap(saved_service):
    _, app, provider, _ = saved_service
    uid = user_id(saved_service)
    ids = []
    for index in range(501):
        doc = app.state.catalog.put(
            "song",
            f"{index:011d}",
            {"title": f"Saved {index:03d}", "isDir": False},
        )
        ids.append(doc["id"])
    with app.state.accounts.transaction() as connection:
        connection.executemany(
            "INSERT INTO favorites VALUES(?,?,?)",
            [(uid, item, "2026-10-29T00:00:00Z") for item in ids],
        )
        connection.executemany(
            "INSERT INTO favorite_tracks VALUES(?,?,?)",
            [(uid, item, item) for item in ids],
        )
    first = body(get(saved_service, "search3", query="", songCount=500))[
        "searchResult3"
    ]["song"]
    second = body(
        get(saved_service, "search3", query="", songCount=500, songOffset=500)
    )["searchResult3"]["song"]
    assert len(first) == 500 and len(second) == 1
    assert len({song["id"] for song in first + second}) == 501
    assert not provider.calls


def test_private_index_revision_changes_on_unlike_and_not_on_duplicate_star(
    saved_service,
):
    _, _, _, keys = saved_service
    song = search(saved_service)["song"][0]["id"]

    def revision(**params):
        return body(get(saved_service, "getIndexes", **params))["indexes"][
            "lastModified"
        ]

    assert revision() == revision(apiKey=keys["alice"]) == 0
    get(saved_service, "star", id=song)
    changed = revision()
    assert changed > 0 and revision(apiKey=keys["alice"]) == 0
    get(saved_service, "star", id=song)
    assert revision() == changed
    get(saved_service, "unstar", id=song)
    assert revision() > changed


def test_job_intent_commits_with_heart_before_cancellable_reconciliation(
    tmp_path, monkeypatch
):
    async def scenario():
        accounts, _, library, audio, song = make_store(tmp_path)
        uid = accounts.user("admin")["id"]
        original = library.reconcile

        async def interrupted(*args, **kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr(library, "reconcile", interrupted)
        with pytest.raises(asyncio.CancelledError):
            await library.change(uid, [(song["id"], None)], True)
        assert library.stars(uid)
        assert library.downloads(uid)[0]["state"] == "pending"
        monkeypatch.setattr(library, "reconcile", original)
        await library.reconcile(startup=True)
        await library.process_one()
        assert library.downloads(uid)[0]["state"] == "complete"
        await audio.close()

    asyncio.run(scenario())


def test_runtime_damage_does_not_silently_delete_saved_audio(tmp_path):
    async def scenario():
        accounts, _, library, audio, song = make_store(tmp_path)
        uid = accounts.user("admin")["id"]
        await library.change(uid, [(song["id"], None)], True)
        await library.process_one()
        key = f"{SOURCE}-aac"
        path = audio.path(key, audio.entries[key])
        path.write_bytes(b"damaged")
        with pytest.raises(CatalogError, match="needs repair"):
            await audio.acquire(SOURCE)
        assert path.read_bytes() == b"damaged"
        assert audio.entries[key]["saved"] and library.stars(uid)
        await audio.close()

    asyncio.run(scenario())
