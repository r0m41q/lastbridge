import copy
import time

import pytest
from support.imports import accept, due, user
from support.library import run

from lastbridge.features.playlists.domain.imports import DAY
from lastbridge.shared.errors import CatalogError


def test_unchanged_refresh_skips_resolution_and_keeps_changed(
    imported_service,
):
    service, imports, state, calls = imported_service
    chart = accept(imported_service, shared=True)
    owner = user(service)
    before = imports.get(owner, chart)
    document = imports.document(owner, before)
    original_batch = imports.provider.batch

    async def unexpected_batch(progress):
        raise AssertionError("Unchanged playlist must not resolve again")

    imports.provider.batch = unexpected_batch
    due(service, chart)
    assert run(service, imports.refresh_one)
    after = imports.get(owner, chart)
    assert after["state"] == "complete"
    assert len(calls) == 2
    assert after["entries"] == before["entries"]
    assert after["updated"] == before["updated"]
    assert after["last_checked"] > before["last_checked"]
    assert imports.document(owner, after)["changed"] == document["changed"]
    assert after["next_attempt"] == after["last_checked"] + DAY
    # A title-only change needs no track searches but changes the client date.
    state["title"] = "New chart name"
    due(service, chart)
    assert run(service, imports.refresh_one)
    renamed = imports.get(owner, chart)
    assert renamed["state"] == "complete"
    assert renamed["updated"] > after["updated"]
    # Explicit refresh is a deliberate retry of matching.
    batches = []

    async def batch(progress):
        batches.append(True)
        return await original_batch(progress)

    imports.provider.batch = batch
    run(service, imports.refresh, owner, chart)
    assert run(service, imports.refresh_one)
    assert batches
    assert imports.get(owner, chart)["updated"] == renamed["updated"]


@pytest.mark.parametrize("change", ["order", "metadata", "removed"])
def test_changed_tracks_invalidate_snapshot(imported_service, change):
    service, imports, state, _ = imported_service
    chart = accept(imported_service, shared=True)
    owner = user(service)
    before = imports.get(owner, chart)
    state["tracks"] = copy.deepcopy(state["tracks"])
    if change == "order":
        state["tracks"].reverse()
    elif change == "metadata":
        state["tracks"][0]["title"] = "Another song"
    else:
        state["tracks"].pop()
        state["trackCount"] -= 1
    batches = []
    original = imports.provider.batch

    async def batch(progress):
        batches.append(True)
        return await original(progress)

    imports.provider.batch = batch
    due(service, chart)
    assert run(service, imports.refresh_one)
    after = imports.get(owner, chart)
    assert after["state"] == "complete" and batches
    assert after["entries"] != before["entries"]
    assert after["updated"] > before["updated"]


def test_interval_uses_last_check_and_preserves_active_lease(imported_service):
    service, imports, _, _ = imported_service
    chart = accept(imported_service, shared=True)
    owner = user(service)
    before = imports.get(owner, chart)
    imports.schedule_shared(owner, chart, "7")
    scheduled = imports.get(owner, chart)
    assert scheduled["refresh_days"] == 7
    assert scheduled["next_attempt"] == before["last_checked"] + 7 * DAY
    assert scheduled["updated"] == before["updated"]
    assert imports.repository.claim() is None
    job = imports.repository.claim(chart)
    leased = imports.get(owner, chart)
    imports.schedule_shared(owner, chart, "3")
    active = imports.get(owner, chart)
    assert active["token"] == job["token"]
    assert active["next_attempt"] == leased["next_attempt"]
    assert run(service, imports.load, job)
    after = imports.get(owner, chart)
    assert after["next_attempt"] == after["last_checked"] + 3 * DAY
    assert after["updated"] == before["updated"]
    # Shortening an interval makes an overdue chart eligible immediately.
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE playlist_imports SET last_checked=? WHERE id=?",
            (time.time() - 2 * DAY, chart),
        )
    imports.schedule_shared(owner, chart, "1")
    assert imports.repository.claim()["id"] == chart


@pytest.mark.parametrize("days", ["", "0", "-1", "1.5", "36501", "x", "١"])
def test_invalid_interval_is_rejected(imported_service, days):
    service, imports, _, _ = imported_service
    chart = accept(imported_service, shared=True)
    with pytest.raises(CatalogError, match="interval"):
        imports.schedule_shared(user(service), chart, days)
    assert imports.get(user(service), chart)["refresh_days"] == 1


def test_interval_permissions_and_private_isolation(imported_service):
    service, imports, _, _ = imported_service
    chart = accept(imported_service, shared=True)
    private = accept(imported_service)
    with pytest.raises(CatalogError, match="Administrator"):
        imports.schedule_shared(user(service, "alice"), chart, "2")
    for item_id in (private, "missing"):
        with pytest.raises(CatalogError, match="Shared chart not found"):
            imports.schedule_shared(user(service), item_id, "2")


def test_unchanged_refresh_preserves_choices_skips_and_restore(
    imported_service,
):
    from support.catalog import SONG

    service, imports, state, _ = imported_service
    source = SONG | {
        "title": "Instant Crush <studios>",
        "videoType": "MUSIC_VIDEO_TYPE_OMV",
    }
    state.update(tracks=[source, SONG, source], trackCount=3)
    chart = accept(imported_service, shared=True)
    owner = user(service)
    initial = imports.get(owner, chart)
    entry = initial["entries"][0]
    candidate = entry["candidates"][0]
    key = entry["source_key"]
    imports.decide(owner, chart, key, candidate["song_id"])
    for action in (None, "skip", "restore", "clear"):
        if action:
            imports.decide(
                owner,
                chart,
                key,
                "",
                action="choose" if action == "clear" else action,
            )
        before = imports.get(owner, chart)
        due(service, chart)
        assert run(service, imports.refresh_one)
        after = imports.get(owner, chart)
        assert after["state"] == "complete"
        assert after["entries"] == before["entries"]
        assert after["updated"] == before["updated"]
    assert after["entries"] == initial["entries"]


def test_interval_change_keeps_failure_retry_and_old_snapshot(
    imported_service,
):
    service, imports, state, _ = imported_service
    chart = accept(imported_service, shared=True)
    owner = user(service)
    before = imports.get(owner, chart)
    state["fail"] = True
    due(service, chart)
    assert run(service, imports.refresh_one)
    failed = imports.get(owner, chart)
    imports.schedule_shared(owner, chart, "5")
    scheduled = imports.get(owner, chart)
    for key in ("state", "next_attempt", "updated", "last_checked", "entries"):
        assert scheduled[key] == failed[key]
    assert scheduled["last_checked"] == before["last_checked"]
    state["fail"] = False
    due(service, chart)
    assert run(service, imports.refresh_one)
    recovered = imports.get(owner, chart)
    assert recovered["state"] == "complete"
    assert recovered["next_attempt"] == recovered["last_checked"] + 5 * DAY
    assert recovered["updated"] == before["updated"]
