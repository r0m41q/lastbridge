import asyncio
import time

import pytest
from support.audio import AUDIO, SOURCE
from support.build_accounts import make_accounts
from support.build_library import make_library
from support.catalog import SONG, body, get
from support.corrections import (
    ALTERNATE,
    choose_recording,
    import_love,
    search_recordings,
)
from support.imports import accept, browser, user
from support.library import complete_jobs, run, user_id

from lastbridge.shared.errors import CatalogError, StorageError


def test_correction_keeps_ids_favorites_playlists_and_other_users(
    correction_service,
):
    service, original, state = correction_service
    _, app, _, keys = service
    owner = user_id(service)
    get(service, "star", id=original["id"])
    get(service, "star", id=original["id"], apiKey=keys["alice"])
    starred = app.state.services.library.stars(owner)[original["id"]]
    playlist = body(
        get(
            service,
            "createPlaylist",
            name="Saved",
            songId=[original["id"], original["id"]],
        )
    )["playlist"]
    before = app.state.services.catalog.read(original["id"])
    candidate = choose_recording(correction_service)
    assert app.state.services.library.stars(owner)[original["id"]] == starred
    selected = body(get(service, "getSong", id=original["id"]))["song"]
    assert (
        selected["id"] == original["id"]
        and selected["album"] == "Another release"
    )
    assert selected["starred"] == starred
    entries = body(get(service, "getPlaylist", id=playlist["id"]))["playlist"][
        "entry"
    ]
    assert [s["id"] for s in entries] == [original["id"], original["id"]]
    assert all(s["album"] == "Another release" for s in entries)
    assert app.state.services.catalog.read(original["id"]) == before
    alice = body(
        get(service, "getSong", id=original["id"], apiKey=keys["alice"])
    )["song"]
    assert alice["album"] == original["album"]
    assert app.state.services.library.pins() == {
        f"{SOURCE}-aac",
        f"{ALTERNATE}-aac",
    }
    # Source preference and jobs survive a new repository/service instance.
    reopened = make_library(
        app.state.services.accounts, app.state.services.catalog, "aac"
    )
    assert (
        reopened.recording(owner, original["id"])["source"]
        == candidate["source"]
    )
    assert state["calls"] and not app.state.services.audio.provider.calls


def test_stream_download_and_saved_job_use_selected_audio(correction_service):
    service, original, _ = correction_service
    get(service, "star", id=original["id"])
    choose_recording(correction_service)
    assert get(service, "stream", id=original["id"]).content == AUDIO
    assert service[1].state.services.audio.provider.calls == [ALTERNATE]
    assert get(service, "download", id=original["id"]).content == AUDIO
    complete_jobs(service)
    audio = service[1].state.services.audio
    assert audio.entries[f"{ALTERNATE}-aac"]["saved"]
    assert f"{SOURCE}-aac" not in audio.entries
    assert (
        service[1].state.services.library.downloads(user_id(service))[0][
            "state"
        ]
        == "complete"
    )
    dashboard = service[1].state.services.administration.repository.snapshot()
    assert dashboard["retained"][user_id(service)] == {ALTERNATE}


def test_failed_replacement_keeps_favorite_and_retries_selected_source(
    correction_service,
):
    service, original, _ = correction_service
    get(service, "star", id=original["id"])
    choose_recording(correction_service)
    services = service[1].state.services
    services.audio.provider.fail = True
    assert run(service, services.library.process_one)
    assert original["id"] in services.library.stars(user_id(service))
    assert services.audio.provider.calls == [ALTERNATE]
    services.audio.provider.fail = False
    services.library.retry(user_id(service))
    assert run(service, services.library.process_one)
    assert services.audio.provider.calls == [ALTERNATE, ALTERNATE]
    assert (
        services.library.downloads(user_id(service))[0]["state"] == "complete"
    )


def test_unlike_after_correction_clears_the_original_lastfm_love(
    correction_service,
):
    service, original, state = correction_service
    get(service, "star", id=original["id"])
    state["rows"][0]["title"] = "Instant Crush <studio> (Live)"
    choose_recording(correction_service, acknowledge=True)
    get(service, "unstar", id=original["id"])
    with service[1].state.services.database.connection() as connection:
        rows = connection.execute(
            "SELECT artist,title,enabled FROM love_outbox WHERE user_id=?",
            (user_id(service),),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == original["title"]
    assert rows[0]["artist"] == original["artist"]
    assert rows[0]["enabled"] == 0


def test_preview_displays_and_checks_the_retained_recording_metadata(
    correction_service,
):
    service, original, state = correction_service
    catalog = service[1].state.services.catalog
    catalog.song(SONG | {"isExplicit": True})
    cached = catalog.song(state["rows"][0] | {"isExplicit": False})
    catalog.repository.put("song", ALTERNATE, cached, complete=True)
    get(service, "star", id=original["id"])
    _, _, review = search_recordings(correction_service)
    candidate = review["candidates"][0]
    assert candidate["explicit"] is False
    assert candidate["mismatch"]
    assert "Different explicit/clean status" in candidate["warnings"]


def test_restore_original_unlike_and_playlist_retention(correction_service):
    service, original, _ = correction_service
    get(service, "star", id=original["id"])
    playlists = service[1].state.services.playlists
    owner = service[1].state.services.accounts.user("admin")
    playlist = run(
        service, playlists.create, owner, "Pinned", [original["id"]]
    )
    run(service, playlists.keep, owner, playlist["id"], True)
    choose_recording(correction_service)
    get(service, "unstar", id=original["id"])
    assert service[1].state.services.library.pins() == {f"{ALTERNATE}-aac"}
    token = browser(service)
    response = service[0].post(
        f"/library/recordings/song/{original['id']}/clear",
        data={"csrf": token},
    )
    assert response.status_code == 200
    assert service[1].state.services.library.pins() == {f"{SOURCE}-aac"}
    assert (
        body(get(service, "getSong", id=original["id"]))["song"]["album"]
        == original["album"]
    )
    assert not service[1].state.services.library.stars(user_id(service))


@pytest.mark.parametrize(
    "change",
    [
        {"isExplicit": False},
        {"title": "Instant Crush <studio> (Live)"},
        {"title": "Another song"},
        {"duration_seconds": 100},
        {"artists": [{"name": "Another artist", "id": "UC_other"}]},
    ],
)
def test_different_recordings_need_acknowledgment(correction_service, change):
    service, original, state = correction_service
    state["rows"][0].update(change)
    get(service, "star", id=original["id"])
    if "isExplicit" in change:
        service[1].state.services.catalog.song(SONG | {"isExplicit": True})
    token, path, review = search_recordings(correction_service)
    candidate = review["candidates"][0]
    assert candidate["mismatch"] and candidate["warnings"]
    data = {
        "csrf": token,
        "token": review["token"],
        "song_id": candidate["song_id"],
    }
    assert service[0].post(path + "/choose", data=data).status_code == 400
    assert (
        service[1].state.services.library.recording(
            user_id(service), original["id"]
        )["source"]
        == SOURCE
    )
    assert (
        service[0]
        .post(path + "/choose", data=data | {"acknowledge": "yes"})
        .status_code
        == 200
    )


def test_unmatched_lastfm_love_can_be_selected_without_outbound_love(
    correction_service,
):
    service, _, state = correction_service
    key = import_love(service)
    candidate = choose_recording(
        correction_service, kind="love", key=key, acknowledge=True
    )
    owner = user_id(service)
    assert candidate["song_id"] in service[1].state.services.library.stars(
        owner
    )
    with service[1].state.services.database.connection() as connection:
        row = connection.execute(
            "SELECT * FROM loved_items WHERE user_id=?", (owner,)
        ).fetchone()
        assert (
            row["state"] == "matched"
            and row["manual_song_id"] == candidate["song_id"]
        )
        assert (
            connection.execute("SELECT count(*) FROM love_outbox").fetchone()[
                0
            ]
            == 0
        )
    assert service[1].state.services.library.pins() == {f"{ALTERNATE}-aac"}


def test_manual_love_choice_wins_over_in_flight_automatic_match(
    correction_service,
):
    service, original, _ = correction_service
    key = import_love(service)
    sync = service[1].state.services.sync
    with service[1].state.services.database.transaction() as connection:
        connection.execute("UPDATE loved_items SET state='pending'")
    job, item = sync.repository.claim_import()
    candidate = choose_recording(
        correction_service, kind="love", key=key, acknowledge=True
    )

    async def delayed_import():
        return await service[1].state.services.library.change(
            user_id(service),
            [(original["id"], "song")],
            True,
            imported=(job["target"], key),
        )

    assert not run(service, delayed_import)
    sync.repository.resolved(job, item, "matched", None, original["id"])
    with service[1].state.services.database.connection() as connection:
        row = connection.execute(
            "SELECT song_id FROM loved_items WHERE recording_key=?", (key,)
        ).fetchone()
        assert row["song_id"] == candidate["song_id"]
    assert original["id"] not in service[1].state.services.library.stars(
        user_id(service)
    )


def test_saved_choice_resolves_pending_import_and_can_restore_original(
    correction_service,
):
    service, original, _ = correction_service
    get(service, "star", id=original["id"])
    key = import_love(service, song_id=original["id"])
    with service[1].state.services.database.transaction() as connection:
        connection.execute("UPDATE loved_items SET state='pending'")
    choose_recording(correction_service)
    with service[1].state.services.database.connection() as connection:
        row = connection.execute(
            "SELECT state,manual_song_id FROM loved_items "
            "WHERE recording_key=?",
            (key,),
        ).fetchone()
        assert row["state"] == "matched" and row["manual_song_id"]
    token = browser(service)
    response = service[0].post(
        f"/library/recordings/song/{original['id']}/clear",
        data={"csrf": token},
    )
    assert response.status_code == 200
    with service[1].state.services.database.connection() as connection:
        row = connection.execute(
            "SELECT manual_song_id FROM loved_items WHERE recording_key=?",
            (key,),
        ).fetchone()
        assert row["manual_song_id"] is None


def test_corrected_metadata_is_used_for_lyrics_scrobbles_and_prefetch(
    correction_service, monkeypatch
):
    service, original, state = correction_service
    state["rows"][0]["duration_seconds"] = 330
    get(service, "star", id=original["id"])
    choose_recording(correction_service, acknowledge=True)
    services = service[1].state.services
    observed = []

    async def lyrics(song):
        observed.append(song)
        return []

    monkeypatch.setattr(services.lyrics.provider, "get", lyrics)
    assert (
        body(get(service, "getLyricsBySongId", id=original["id"]))["status"]
        == "ok"
    )
    assert observed[0]["album"] == "Another release"
    assert observed[0]["duration"] == 330
    assert (
        body(
            get(
                service,
                "getLyrics",
                artist=original["artist"],
                title=original["title"],
            )
        )["status"]
        == "ok"
    )
    assert len(observed) == 1
    response = get(
        service,
        "scrobble",
        id=[original["id"]],
        time=[str(int(time.time() * 1000) - 500000)],
    )
    assert body(response)["status"] == "ok"
    with services.database.connection() as connection:
        row = connection.execute(
            "SELECT song_id,album,duration FROM scrobbles"
        ).fetchone()
        assert (
            row["song_id"] == original["id"]
            and row["album"] == "Another release"
        )
        assert row["duration"] == 330
    recent = body(get(service, "getAlbumList2", type="recent"))["albumList2"][
        "album"
    ]
    assert (
        recent[0]["id"]
        == services.catalog.read_source(ALTERNATE, "song")["document"][
            "albumId"
        ]
    )
    playlist = body(
        get(
            service, "createPlaylist", name="Versions", songId=[original["id"]]
        )
    )["playlist"]
    assert playlist["duration"] == 330
    other = services.catalog.song(SONG | {"videoId": "othertrack1"})
    response = get(
        service,
        "savePlayQueue",
        id=[other["id"], original["id"]],
        current=other["id"],
    )
    assert body(response)["status"] == "ok"

    async def wait_prefetch():
        await asyncio.gather(*list(services.play_queue.tasks.values()))

    run(service, wait_prefetch)
    assert services.audio.provider.calls == [ALTERNATE]
    queue = body(get(service, "getPlayQueue"))["playQueue"]
    assert queue["entry"][1]["id"] == original["id"]
    assert queue["entry"][1]["album"] == "Another release"


@pytest.mark.parametrize("invalid", ["expired", "stale", "forged", "metadata"])
def test_invalid_previews_preserve_the_existing_choice(
    correction_service, invalid
):
    service, original, state = correction_service
    get(service, "star", id=original["id"])
    token, path, review = search_recordings(correction_service)
    data = {
        "csrf": token,
        "token": review["token"],
        "song_id": review["candidates"][0]["song_id"],
    }
    if invalid == "expired":
        with service[1].state.services.database.transaction() as connection:
            connection.execute("UPDATE recording_reviews SET expires=0")
    elif invalid == "stale":
        search_recordings(correction_service)
        data["csrf"] = browser(service)
    elif invalid == "forged":
        data["song_id"] = original["id"]
    else:
        service[1].state.services.catalog.song(
            state["rows"][0] | {"title": "Changed title"}
        )
    assert service[0].post(path + "/choose", data=data).status_code == 400
    assert (
        service[1].state.services.library.recording(
            user_id(service), original["id"]
        )["source"]
        == SOURCE
    )


def test_ownership_csrf_origin_and_unsaved_recordings(correction_service):
    service, original, _ = correction_service
    path = f"/library/recordings/song/{original['id']}"
    token = browser(service)
    assert service[0].get(path).status_code == 404
    get(service, "star", id=original["id"])
    assert (
        service[0].post(path + "/search", data={"csrf": "wrong"}).status_code
        == 403
    )
    assert (
        service[0]
        .post(
            path + "/search",
            data={"csrf": token},
            headers={"Origin": "https://elsewhere.invalid"},
        )
        .status_code
        == 403
    )
    token, path, review = search_recordings(correction_service)
    other_csrf = browser(service, "alice")
    assert service[0].get(path).status_code == 404
    assert (
        service[0]
        .post(
            path + "/choose",
            data={
                "csrf": other_csrf,
                "token": review["token"],
                "song_id": review["candidates"][0]["song_id"],
            },
        )
        .status_code
        == 404
    )


def test_storage_rollback_does_not_change_preferences_or_retention(
    correction_service, monkeypatch
):
    service, original, _ = correction_service
    get(service, "star", id=original["id"])
    token, path, review = search_recordings(correction_service)

    def fail(connection):
        raise StorageError("secret SQLite location")

    monkeypatch.setattr(
        service[1].state.services.library.repository, "schedule", fail
    )
    response = service[0].post(
        path + "/choose",
        data={
            "csrf": token,
            "token": review["token"],
            "song_id": review["candidates"][0]["song_id"],
        },
    )
    assert response.status_code == 503 and "secret" not in response.text
    assert (
        service[1].state.services.library.recording(
            user_id(service), original["id"]
        )["source"]
        == SOURCE
    )
    assert service[1].state.services.library.pins() == {f"{SOURCE}-aac"}


def test_migration_nineteen_preserves_membership(correction_service):
    service, original, _ = correction_service
    get(service, "star", id=original["id"])
    database = service[1].state.services.database
    with database.transaction() as connection:
        connection.execute("DROP VIEW retained_audio")
        connection.execute("DROP TABLE recording_reviews")
        connection.execute("DROP TABLE recording_corrections")
        connection.execute(
            "ALTER TABLE loved_items DROP COLUMN manual_song_id"
        )
        connection.execute("PRAGMA user_version=19")
    accounts = make_accounts(database.path.parent)
    reopened = make_library(accounts, service[1].state.services.catalog, "aac")
    assert original["id"] in reopened.stars(user_id(service))
    assert (
        reopened.recording(user_id(service), original["id"])["source"]
        == SOURCE
    )
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 22
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_shared_chart_keeps_snapshot_and_applies_private_recording_choice(
    correction_service,
    imported_service,
):
    service, original, state = correction_service
    chart_id = accept(imported_service, shared=True)
    imports = imported_service[1]
    before = imports.get(user(service), chart_id)["entries"]
    get(service, "star", id=original["id"])
    state["rows"][0]["duration_seconds"] = 330
    choose_recording(correction_service, acknowledge=True)
    chart = body(get(service, "getPlaylist", id=chart_id))["playlist"]
    assert chart["entry"][0]["album"] == "Another release"
    assert chart["duration"] == sum(s["duration"] for s in chart["entry"])
    alice = body(
        get(service, "getPlaylist", id=chart_id, apiKey=service[3]["alice"])
    )["playlist"]
    assert alice["entry"][0]["album"] == original["album"]
    assert imports.get(user(service), chart_id)["entries"] == before


@pytest.mark.parametrize("restriction", ["removed", "skipped", "relinked"])
def test_love_choice_respects_removals_and_linked_account(
    correction_service,
    restriction,
):
    service, _, _ = correction_service
    key = import_love(service)
    owner = user_id(service)
    with service[1].state.services.database.transaction() as connection:
        if restriction == "removed":
            connection.execute(
                "INSERT INTO love_removals VALUES(?,?)", (owner, key)
            )
        elif restriction == "skipped":
            connection.execute("UPDATE loved_items SET state='skipped'")
        else:
            connection.execute(
                "UPDATE users SET lastfm_name='another-account' WHERE id=?",
                (owner,),
            )
    browser(service)
    assert service[0].get(f"/library/recordings/love/{key}").status_code == 404


def test_concurrent_source_change_cannot_publish_a_stale_preview(
    correction_service,
    monkeypatch,
):
    service, original, state = correction_service
    get(service, "star", id=original["id"])
    services = service[1].state.services

    async def scenario():
        started, gate = asyncio.Event(), asyncio.Event()

        async def lookup(payload):
            started.set()
            await gate.wait()
            return {"songs": state["rows"]}

        monkeypatch.setattr(services.corrections.provider, "fetch", lookup)
        task = asyncio.create_task(
            services.corrections.search(
                user_id(service), "song", original["id"]
            )
        )
        await started.wait()
        services.catalog.song(SONG | {"title": "Updated title"})
        gate.set()
        with pytest.raises(CatalogError, match="changed"):
            await task
        assert (
            services.library.repository.recording_review(
                user_id(service), "song", original["id"]
            )
            is None
        )

    run(service, scenario)
