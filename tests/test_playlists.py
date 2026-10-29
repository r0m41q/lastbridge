import asyncio
import json
from urllib.parse import urlencode
from xml.etree.ElementTree import fromstring

import pytest
import test_library
from test_accounts import drop_playlist_tables
from test_audio import AUDIO
from test_catalog import body, get, search
from test_library import complete_jobs, run, user_id

from lastbridge.accounts import Accounts
from lastbridge.audio import AudioCache
from lastbridge.library import Library
from lastbridge.playlists import Playlists
from lastbridge.web import COOKIE

saved_service = test_library.saved_service
catalog_service = test_library.catalog_service


def songs(service):
    song = search(service)["song"][0]
    album = body(get(service, "getAlbum", id=song["albumId"]))["album"]
    return [s["id"] for s in album["song"]]


def create(service, ids=(), name="My playlist", **params):
    return body(
        get(service, "createPlaylist", name=name, songId=list(ids), **params)
    )["playlist"]


def detail(service, item_id, **params):
    return body(get(service, "getPlaylist", id=item_id, **params))["playlist"]


def row_count(service, table):
    with service[1].state.accounts.connection() as connection:
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[
            0
        ]


def browser(service, name="admin"):
    client, app, _, _ = service
    token = app.state.accounts.login(
        name, "a sufficiently long password", "test"
    )
    client.cookies.set(COOKIE, token)
    return app.state.accounts.session(token)["csrf"]


def post(service, method, **params):
    return service[0].post(
        "/rest/" + method + ".view",
        content=urlencode(
            {
                "apiKey": service[3]["admin"],
                "v": "1.16.1",
                "c": "test",
                "f": "json",
                **params,
            },
            doseq=True,
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )


def keep(service, item_id, enabled=True, name="admin"):
    app = service[1]
    run(
        service,
        app.state.playlists.keep,
        app.state.accounts.user(name),
        item_id,
        enabled,
    )


def test_create_order_duplicates_xml_and_metadata_only(saved_service):
    service = saved_service
    ids = songs(service)
    item = create(service, [ids[1], ids[0], ids[1]], "Evening <&> ♪")
    assert [s["id"] for s in item["entry"]] == [ids[1], ids[0], ids[1]]
    assert item["songCount"] == 3 and item["duration"] == 1011
    assert item["readonly"] is False and item["public"] is False
    assert item["owner"] == "admin"
    assert row_count(service, "save_jobs") == 0
    assert row_count(service, "favorites") == 0
    assert not service[1].state.audio.provider.calls
    root = fromstring(
        get(service, "getPlaylist", id=item["id"], f="xml").content
    )
    playlist = root.find("{*}playlist")
    assert playlist.attrib["name"] == "Evening <&> ♪"
    assert [r.attrib["id"] for r in playlist.findall("{*}entry")] == [
        ids[1],
        ids[0],
        ids[1],
    ]
    assert (
        body(get(service, "getUser", username="admin"))["user"]["playlistRole"]
        is True
    )
    assert service[1].state.library.collection(user_id(service))["song"] == []


def test_form_replace_append_remove_and_rename_atomically(saved_service):
    service = saved_service
    ids = songs(service)
    item = create(service, [ids[0], ids[1], ids[0], ids[1]])
    response = post(
        service,
        "updatePlaylist",
        playlistId=item["id"],
        songIndexToRemove=[0, 2, 2],
        songIdToAdd=[ids[1], ids[0]],
        name="Renamed",
        comment="Comment <&> ♪",
        public="false",
    )
    assert body(response)["status"] == "ok"
    updated = detail(service, item["id"])
    assert updated["name"] == "Renamed"
    assert updated["comment"] == "Comment <&> ♪"
    assert updated["created"] == item["created"]
    assert updated["changed"] > item["changed"]
    assert [s["id"] for s in updated["entry"]] == [
        ids[1],
        ids[1],
        ids[1],
        ids[0],
    ]
    replacement = body(
        post(
            service,
            "createPlaylist",
            playlistId=item["id"],
            songId=[ids[0], ids[1]],
        )
    )["playlist"]
    assert replacement["name"] == "Renamed"
    assert replacement["comment"] == "Comment <&> ♪"
    assert [s["id"] for s in replacement["entry"]] == ids
    cleared = body(post(service, "createPlaylist", playlistId=item["id"]))[
        "playlist"
    ]
    assert cleared["entry"] == [] and cleared["songCount"] == 0


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"songIndexToRemove": ["2"]}, 10),
        ({"songIndexToRemove": ["-1"]}, 10),
        ({"songIndexToRemove": ["0.5"]}, 10),
        ({"songIndexToRemove": ["١"]}, 10),
        ({"songIdToAdd": ["unknown"]}, 70),
        ({"name": " "}, 10),
        ({"name": "x" * 201}, 10),
        ({"comment": "\x01"}, 10),
        ({"public": "yes"}, 10),
        ({"public": "true"}, 50),
    ],
)
def test_invalid_edit_keeps_entire_snapshot(saved_service, changes, code):
    service = saved_service
    ids = songs(service)
    item = create(service, ids)
    response = get(
        service,
        "updatePlaylist",
        playlistId=item["id"],
        **({"name": "Must not stick"} | changes),
    )
    assert body(response)["error"]["code"] == code
    assert detail(service, item["id"]) == item
    assert row_count(service, "save_jobs") == 0


def test_invalid_create_bounds_and_parameter_batches(saved_service):
    service = saved_service
    ids = songs(service)
    for params, code in [
        ({}, 10),
        ({"name": "\x00"}, 10),
        ({"name": "x", "songId": [ids[0], "missing"]}, 70),
        ({"name": "x", "playlistId": ""}, 10),
        ({"name": "x", "songId": [ids[0]] * 1001}, 10),
    ]:
        assert (
            body(get(service, "createPlaylist", **params))["error"]["code"]
            == code
        )
    assert row_count(service, "playlists") == 0
    album = body(get(service, "getSong", id=ids[0]))["song"]["albumId"]
    assert (
        body(get(service, "createPlaylist", name="x", songId=album))["error"][
            "code"
        ]
        == 70
    )
    # Exceeds the old 128-field/16KiB form limit; duplicates preserved.
    item = body(
        post(service, "createPlaylist", name="Large", songId=[ids[0]] * 1000)
    )["playlist"]
    assert item["songCount"] == 1000
    assert (
        body(
            post(
                service,
                "updatePlaylist",
                playlistId=item["id"],
                songIdToAdd=ids,
            )
        )["error"]["code"]
        == 10
    )
    assert detail(service, item["id"])["songCount"] == 1000
    duplicate = service[0].get(
        "/rest/createPlaylist",
        params=[
            ("apiKey", service[3]["admin"]),
            ("v", "1"),
            ("c", "test"),
            ("name", "first"),
            ("name", "second"),
        ],
    )
    assert b'code="10"' in duplicate.content


def test_owner_isolation_generated_readonly_and_head(saved_service):
    service = saved_service
    ids = songs(service)
    item = create(service, ids, apiKey=service[3]["alice"])
    for method, params in [
        ("getPlaylist", {"id": item["id"]}),
        ("createPlaylist", {"playlistId": item["id"], "songId": ids}),
        ("updatePlaylist", {"playlistId": item["id"], "name": "steal"}),
        ("deletePlaylist", {"id": item["id"]}),
    ]:
        assert body(get(service, method, **params))["error"]["code"] == 70
    listing = body(get(service, "getPlaylists"))["playlists"]["playlist"]
    assert item["id"] not in [p["id"] for p in listing]
    generated = listing[0]
    assert generated["readonly"] is True
    for method, params in [
        ("createPlaylist", {"playlistId": generated["id"]}),
        ("updatePlaylist", {"playlistId": generated["id"]}),
        ("deletePlaylist", {"id": generated["id"]}),
    ]:
        assert body(get(service, method, **params))["error"]["code"] == 50
    for method in ("createPlaylist", "updatePlaylist", "deletePlaylist"):
        response = service[0].head(
            "/rest/" + method,
            params={
                "apiKey": service[3]["alice"],
                "v": "1",
                "c": "test",
                "name": "No new list",
                "id": item["id"],
                "playlistId": item["id"],
            },
        )
        assert response.status_code == 200
    assert detail(service, item["id"], apiKey=service[3]["alice"]) == item
    assert row_count(service, "playlists") == 1
    assert (
        body(get(service, "getPlaylists", username="alice"))["error"]["code"]
        == 50
    )
    assert (
        body(
            get(
                service,
                "createPlaylist",
                name="Unlinked",
                apiKey=service[3]["unlinked"],
            )
        )["error"]["code"]
        == 50
    )


def test_kept_playlists_share_audio_and_do_not_create_hearts(saved_service):
    service = saved_service
    ids = songs(service)
    first = create(service, [ids[0], ids[0]])
    second = create(service, ids, apiKey=service[3]["alice"])
    keep(service, first["id"])
    keep(service, second["id"], name="alice")
    complete_jobs(service)
    app = service[1]
    assert len(app.state.audio.provider.calls) == 2
    assert all(e.get("saved", False) for e in app.state.audio.entries.values())
    assert row_count(service, "favorites") == 0
    assert len(app.state.library.collection(user_id(service))["song"]) == 1
    assert len(app.state.library.downloads(user_id(service))) == 1
    assert body(get(service, "getStarred2"))["starred2"]["song"] == []
    get(service, "star", id=ids[0])
    keep(service, first["id"], False)
    get(service, "deletePlaylist", id=second["id"], apiKey=service[3]["alice"])
    source = app.state.catalog.read(ids[0])["source"]
    key = f"{source}-aac"
    assert app.state.audio.entries[key]["saved"]
    assert (
        sum(
            bool(e.get("saved", False))
            for e in app.state.audio.entries.values()
        )
        == 1
    )
    get(service, "unstar", id=ids[0])
    assert not app.state.audio.entries[key]["saved"]
    assert row_count(service, "save_jobs") == 0
    assert detail(service, first["id"])["songCount"] == 2
    assert app.state.library.collection(user_id(service))["song"] == []


def test_kept_edit_releases_old_pins_and_queues_new_tracks(saved_service):
    service = saved_service
    ids = songs(service)
    item = create(service, [ids[0]])
    keep(service, item["id"])
    complete_jobs(service)
    app = service[1]
    before = app.state.accounts.user("admin")["library_changed"]
    replacement = body(
        post(service, "createPlaylist", playlistId=item["id"], songId=[ids[1]])
    )["playlist"]
    assert replacement["entry"][0]["id"] == ids[1]
    assert app.state.accounts.user("admin")["library_changed"] > before
    assert (
        app.state.library.downloads(user_id(service))[0]["state"] == "pending"
    )
    assert not any(
        e.get("saved", False) for e in app.state.audio.entries.values()
    )
    complete_jobs(service)
    assert (
        sum(
            bool(e.get("saved", False))
            for e in app.state.audio.entries.values()
        )
        == 1
    )
    assert (
        body(get(service, "deletePlaylist", id=item["id"]))["status"] == "ok"
    )
    assert not app.state.library.pins()
    assert row_count(service, "playlist_entries") == 0
    assert not any(
        e.get("saved", False) for e in app.state.audio.entries.values()
    )


def test_release_during_transfer_keeps_loaded_queue_and_lease(saved_service):
    service = saved_service
    app = service[1]
    ids = songs(service)
    item = create(service, ids)
    queued = detail(service, item["id"])["entry"]
    keep(service, item["id"])

    async def exercise():
        provider = app.state.audio.provider
        entered, release = asyncio.Event(), asyncio.Event()
        original = provider.fetch

        async def blocked(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        provider.fetch = blocked
        task = asyncio.create_task(app.state.library.process_one())
        await entered.wait()
        await app.state.playlists.delete(
            app.state.accounts.user("admin"), item["id"]
        )
        release.set()
        assert await task
        assert not app.state.audio.pins
        assert not any(
            e.get("saved", False) for e in app.state.audio.entries.values()
        )
        # Already returned queue identities still resolve and play.
        key, entry = await app.state.audio.acquire(
            app.state.catalog.read(queued[0]["id"])["source"]
        )
        assert app.state.audio.path(key, entry).read_bytes() == AUDIO
        await app.state.audio.release(key)

    run(service, exercise)
    assert [s["id"] for s in queued] == ids
    assert row_count(service, "save_jobs") == 0


def test_failed_download_retry_and_restart_pin_recovery(saved_service):
    service = saved_service
    app = service[1]
    item = create(service, songs(service))
    keep(service, item["id"])
    provider = app.state.audio.provider
    provider.fail = True
    run(service, app.state.library.process_one)
    assert any(
        r["error"] for r in app.state.library.downloads(user_id(service))
    )
    app.state.library.retry(user_id(service))
    provider.fail = False
    complete_jobs(service)
    directory = app.state.accounts.path.parent
    run(service, app.state.audio.close)
    restored = Accounts(directory)
    library = Library(restored, app.state.catalog, "aac")

    async def restart():
        audio = AudioCache(
            directory / "cache" / "audio",
            saved_directory=directory / "saved" / "audio",
            pins=library.pins(),
            policy="aac",
        )
        library.audio = audio
        await library.reconcile(startup=True)
        assert len(audio.entries) == 2
        assert all(e.get("saved", False) for e in audio.entries.values())
        playlists = Playlists(
            restored,
            app.state.catalog,
            library,
            app.state.mixes,
            app.state.radio,
        )
        assert (
            playlists.get(restored.user("admin"), item["id"])["songCount"] == 2
        )
        await audio.close()

    run(service, restart)


def test_release_keeps_a_leased_saved_file_readable(saved_service):
    service = saved_service
    app = service[1]
    song_id = songs(service)[0]
    item = create(service, [song_id])
    keep(service, item["id"])
    complete_jobs(service)

    async def exercise():
        key, entry = await app.state.audio.acquire(
            app.state.catalog.read(song_id)["source"]
        )
        path = app.state.audio.path(key, entry)
        await app.state.playlists.delete(
            app.state.accounts.user("admin"), item["id"]
        )
        assert app.state.audio.entries[key]["saved"]
        assert path.read_bytes() == AUDIO
        await app.state.audio.release(key)
        assert not app.state.audio.entries[key]["saved"]
        assert app.state.audio.path(key, app.state.audio.entries[key]).exists()

    run(service, exercise)


def test_browser_controls_csrf_copy_and_private_snapshot(saved_service):
    service = saved_service
    client, app, _, keys = service
    assert client.get("/playlists", follow_redirects=False).status_code == 303
    ids = songs(service)
    csrf = browser(service)
    run(service, app.state.mixes.close)
    with app.state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='example' WHERE username='admin'"
        )
    user = app.state.accounts.user("admin")
    app.state.mixes.linked(user["id"])
    with app.state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE mixes SET snapshot=? WHERE user_id=?",
            (json.dumps({"comfort": ids}), user["id"]),
        )
    source = app.state.mixes.playlists(user)[0]
    assert source["songCount"] == 2
    assert (
        client.post(
            "/playlists/copy", data={"source": source["id"], "name": "Copy"}
        ).status_code
        == 403
    )
    response = client.post(
        "/playlists/copy",
        data={"csrf": csrf, "source": source["id"], "name": "Copy <&>"},
    )
    assert (
        response.status_code == 200 and "Copy &lt;&amp;&gt;" in response.text
    )
    item = app.state.playlists.local(user)[0]
    assert item["readonly"] is False
    assert detail(service, item["id"])["entry"]
    assert row_count(service, "save_jobs") == 0
    assert (
        client.post(
            f"/playlists/{item['id']}/keep",
            data={"csrf": csrf},
            headers={"origin": "https://foreign.example"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/playlists/{item['id']}/keep", data={"csrf": csrf}
        ).status_code
        == 200
    )
    assert app.state.library.pins()
    with app.state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE mixes SET snapshot='{}' WHERE user_id=?", (user["id"],)
        )
    assert [s["id"] for s in detail(service, item["id"])["entry"]] == ids
    alice_csrf = browser(service, "alice")
    assert "Copy &lt;&amp;&gt;" not in client.get("/playlists").text
    assert (
        client.post(
            f"/playlists/{item['id']}/release", data={"csrf": alice_csrf}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/playlists/copy",
            data={
                "csrf": alice_csrf,
                "source": source["id"],
                "name": "Stolen",
            },
        ).status_code
        == 400
    )
    csrf = browser(service)
    assert (
        client.post(
            f"/playlists/{item['id']}/release", data={"csrf": csrf}
        ).status_code
        == 200
    )
    assert not app.state.library.pins()
    assert (
        client.post(
            f"/playlists/{item['id']}/delete", data={"csrf": csrf}
        ).status_code
        == 200
    )
    assert not app.state.playlists.local(user)
    browser(service, "unlinked")
    assert client.get("/playlists").status_code == 403
    assert (
        client.post(
            "/playlists/create",
            data={
                "csrf": app.state.accounts.session(client.cookies.get(COOKIE))[
                    "csrf"
                ],
                "name": "No",
            },
        ).status_code
        == 403
    )
    assert keys


def test_copy_radio_is_independent_and_does_not_warm_audio(saved_service):
    service = saved_service
    app = service[1]
    ids = songs(service)
    user = app.state.accounts.user("admin")
    with app.state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='example' WHERE id=?", (user["id"],)
        )
        connection.execute(
            "INSERT INTO radio VALUES(?,?,?,?,?,?)",
            (user["id"], "example", ids[0], json.dumps([ids[1]]), 1, 2),
        )
    user = app.state.accounts.user("admin")
    item = run(
        service,
        app.state.playlists.copy,
        user,
        app.state.radio.playlist_id(user["id"]),
        "Radio copy",
    )
    assert [s["id"] for s in item["entry"]] == [ids[1]]
    with app.state.accounts.transaction() as connection:
        connection.execute("DELETE FROM radio")
    assert detail(service, item["id"])["entry"] == item["entry"]
    assert not app.state.audio.provider.calls


def test_seven_to_eight_upgrade_preserves_state(
    saved_service,
):
    service = saved_service
    app = service[1]
    song_id = songs(service)[0]
    get(service, "star", id=song_id)
    store = app.state.accounts
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO lyrics VALUES(?,?,?,?,?)",
            (song_id, "fingerprint", "[]", 10, 1),
        )
        drop_playlist_tables(connection)
        connection.execute("PRAGMA user_version=7")
    migrated = Accounts(store.path.parent)
    assert migrated.client_user({"apiKey": service[3]["admin"]})
    with migrated.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert (
            connection.execute("SELECT * FROM lyrics").fetchone()[
                "fingerprint"
            ]
            == "fingerprint"
        )
        assert connection.execute("SELECT * FROM favorites").fetchone()
        assert connection.execute("SELECT * FROM save_jobs").fetchone()
        assert connection.execute("SELECT * FROM retained_tracks").fetchone()
        assert not connection.execute("SELECT * FROM playlists").fetchall()


def test_playlist_limit_is_atomic(saved_service, monkeypatch):
    import lastbridge.playlists as module

    monkeypatch.setattr(module, "MAX_PLAYLISTS", 1)
    create(saved_service)
    response = get(saved_service, "createPlaylist", name="Too many")
    assert body(response)["error"]["code"] == 10
    assert row_count(saved_service, "playlists") == 1


def test_cancellation_after_commit_retains_download_intent(saved_service):
    service = saved_service
    app = service[1]
    item = create(service, songs(service))

    async def interrupted():
        original = app.state.library.reconcile

        async def cancel():
            raise asyncio.CancelledError

        app.state.library.reconcile = cancel
        try:
            with pytest.raises(asyncio.CancelledError):
                await app.state.playlists.keep(
                    app.state.accounts.user("admin"), item["id"], True
                )
        finally:
            app.state.library.reconcile = original
        assert row_count(service, "save_jobs") == 2
        assert len(app.state.library.pins()) == 2
        await original(startup=True)
        assert app.state.audio.pins == app.state.library.pins()

    run(service, interrupted)


def test_old_reconciliation_cannot_restore_a_released_job(saved_service):
    service = saved_service
    app = service[1]
    item = create(service, [songs(service)[0]])
    keep(service, item["id"])

    async def exercise():
        # An older reconciliation waits for storage while a newer deletion
        # commits and then disconnects. It must use the latest durable intent.
        await app.state.audio.lock.acquire()
        older = asyncio.create_task(app.state.library.reconcile())
        await asyncio.sleep(0)
        deletion = asyncio.create_task(
            app.state.playlists.delete(
                app.state.accounts.user("admin"), item["id"]
            )
        )
        await asyncio.sleep(0)
        deletion.cancel()
        with pytest.raises(asyncio.CancelledError):
            await deletion
        app.state.audio.lock.release()
        await older
        assert not app.state.audio.pins
        assert row_count(service, "save_jobs") == 0

    run(service, exercise)
