import copy

import pytest
from support.catalog import SONG
from support.imports import URL, browser, user
from support.library import run

from lastbridge.features.playlists.domain.imports import Snapshot

JSON = {"Accept": "application/json"}


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("shared", [False, True])
def test_skip_restore_undo_is_immediate_scoped_and_persistent(
    imported_service, published, shared
):
    service, imports, state, calls = imported_service
    owner = user(service)
    state.update(tracks=[SONG, SONG], trackCount=2)
    item_id = run(service, imports.preview, owner, URL, shared)
    run(service, imports.refresh_one)
    if published:
        imports.confirm(owner, item_id)
    before = imports.get(owner, item_id)
    token = browser(service)
    entry = before["entries"][0]
    path = f"/playlist-imports/{item_id}"

    def change(action, **values):
        response = service[0].post(
            path + "/" + action,
            headers=JSON,
            data={"csrf": token, "source_key": entry["source_key"], **values},
        )
        assert response.status_code == 200, response.text
        assert not response.history
        return response.json()

    skipped = change("skip")
    assert all(e["status"] == "skipped" for e in skipped["model"]["entries"])
    after = imports.get(owner, item_id)
    for field in (
        "state",
        "phase",
        "token",
        "processed",
        "total",
        "next_attempt",
        "queued_at",
        "ready",
    ):
        assert after[field] == before[field]
    assert imports.document(owner, after, True)["entry"] == []
    assert len(calls) == 1
    restored = change("restore")
    assert all(e["status"] == "resolved" for e in restored["model"]["entries"])
    change("undo", undo=restored["undo"])
    assert imports.get(owner, item_id)["matched"] == 0
    assert (
        service[0]
        .post(
            path + "/undo",
            headers=JSON,
            data={
                "csrf": token,
                "source_key": entry["source_key"],
                "undo": skipped["undo"],
            },
        )
        .status_code
        == 400
    )
    if not published:
        imports.confirm(owner, item_id)
    run(service, imports.refresh, owner, item_id)
    run(service, imports.refresh_one)
    assert imports.get(owner, item_id)["matched"] == 0
    assert all(e["skipped"] for e in imports.get(owner, item_id)["entries"])
    change("restore")
    assert imports.get(owner, item_id)["matched"] == 2
    assert len(calls) == 2


def test_skip_and_restore_win_over_in_flight_worker(imported_service):
    service, imports, state, _ = imported_service
    owner = user(service)
    state.update(tracks=[SONG], trackCount=1)
    item_id = run(service, imports.preview, owner, URL)
    run(service, imports.refresh_one)
    imports.confirm(owner, item_id)
    job = imports.repository.claim(item_id)
    progress = run(service, imports.provider.prepare, job["source"], {})
    progress = run(service, imports.provider.batch, progress)
    imports.repository.checkpoint(job, progress)
    job["progress"] = copy.deepcopy(progress)
    entry = progress["entries"][0]
    imports.decide(owner, item_id, entry["source_key"], "", action="skip")
    assert imports.repository.checkpoint(job, progress)
    assert imports.get(owner, item_id)["progress"]["entries"][0]["skipped"]
    imports.decide(owner, item_id, entry["source_key"], "", action="restore")
    assert imports.repository.publish(
        job, Snapshot("Chart", progress["entries"])
    )
    final = imports.get(owner, item_id)
    assert final["matched"] == 1 and not final["entries"][0].get("skipped")


def test_json_review_guards_and_safe_templates(imported_service):
    service, imports, _, _ = imported_service
    owner = user(service)
    item_id = run(service, imports.preview, owner, URL, True)
    run(service, imports.refresh_one)
    imports.confirm(owner, item_id)
    path = f"/playlist-imports/{item_id}"
    token = browser(service)
    page = service[0].get(path)
    assert 'http-equiv="refresh"' not in page.text
    assert 'type="checkbox"' not in page.text
    assert 'src="/assets/recording-review.js"' in page.text
    assert service[0].get("/assets/recording-review.js").status_code == 200
    model = service[0].get(path, headers=JSON).json()
    assert model["editable"]
    key = model["entries"][0]["key"]
    assert (
        service[0]
        .post(
            path + "/skip",
            headers=JSON,
            data={"csrf": "wrong", "source_key": key},
        )
        .status_code
        == 403
    )
    assert (
        service[0]
        .post(
            path + "/skip",
            headers=JSON | {"Origin": "https://foreign.test"},
            data={"csrf": token, "source_key": key},
        )
        .status_code
        == 403
    )
    token = browser(service, "alice")
    assert not service[0].get(path, headers=JSON).json()["editable"]
    assert (
        service[0]
        .post(
            path + "/skip",
            headers=JSON,
            data={"csrf": token, "source_key": key},
        )
        .status_code
        == 403
    )


def test_restoring_a_skip_preserves_the_previous_manual_recording(
    imported_service,
):
    service, imports, state, _ = imported_service
    owner = user(service)
    state.update(
        tracks=[SONG | {"videoType": "MUSIC_VIDEO_TYPE_OMV"}], trackCount=1
    )
    item_id = run(service, imports.preview, owner, URL)
    run(service, imports.refresh_one)
    entry = imports.get(owner, item_id)["entries"][0]
    candidate = entry["candidates"][0]
    imports.decide(owner, item_id, entry["source_key"], candidate["song_id"])
    imports.decide(owner, item_id, entry["source_key"], "", action="skip")
    imports.confirm(owner, item_id)
    run(service, imports.refresh, owner, item_id)
    run(service, imports.refresh_one)
    imports.decide(owner, item_id, entry["source_key"], "", action="restore")
    restored = imports.get(owner, item_id)["entries"][0]
    assert restored["reviewed"] and restored["song_id"] == candidate["song_id"]
    assert not restored.get("skipped")


def test_legacy_unavailable_rows_can_be_skipped_without_reimport(
    imported_service,
):
    import json

    service, imports, _, calls = imported_service
    owner = user(service)
    item_id = run(service, imports.preview, owner, URL)
    run(service, imports.refresh_one)
    with service[1].state.services.database.transaction() as connection:
        row = connection.execute(
            "SELECT entries FROM playlist_imports WHERE id=?", (item_id,)
        ).fetchone()
        entries = json.loads(row[0])
        entries[1].pop("source_key")
        connection.execute(
            "UPDATE playlist_imports SET entries=? WHERE id=?",
            (json.dumps(entries), item_id),
        )
    token = browser(service)
    response = service[0].post(
        f"/playlist-imports/{item_id}/skip",
        headers=JSON,
        data={"csrf": token, "source_key": "unavailable:2"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["model"]["entries"][1]["status"] == "skipped"
    assert len(calls) == 1


def test_chart_skip_remembers_a_track_that_leaves_and_returns(
    imported_service,
):
    service, imports, state, _ = imported_service
    owner = user(service)
    state.update(tracks=[SONG], trackCount=1)
    item_id = run(service, imports.preview, owner, URL, True)
    run(service, imports.refresh_one)
    entry = imports.get(owner, item_id)["entries"][0]
    imports.decide(owner, item_id, entry["source_key"], "", action="skip")
    imports.confirm(owner, item_id)
    state.update(tracks=[], trackCount=0)
    run(service, imports.refresh, owner, item_id)
    run(service, imports.refresh_one)
    assert not imports.get(owner, item_id)["entries"]
    state.update(tracks=[SONG], trackCount=1)
    run(service, imports.refresh, owner, item_id)
    run(service, imports.refresh_one)
    assert imports.get(owner, item_id)["entries"][0]["skipped"]
