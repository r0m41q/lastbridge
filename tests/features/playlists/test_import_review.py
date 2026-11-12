import asyncio
import copy

import pytest
from support.build_accounts import make_accounts
from support.catalog import SONG
from support.imports import URL, browser, user
from support.library import run

from lastbridge.features.playlists.adapters.sqlite.imports import (
    ImportRepository,
)
from lastbridge.features.playlists.domain.imports import Snapshot


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("published", [False, True])
def test_large_playlist_selection_is_immediate_without_provider_requests(
    imported_service, shared, published
):
    service, imports, state, calls = imported_service
    owner = user(service)
    source = SONG | {
        "title": "Instant Crush <studios>",
        "videoType": "MUSIC_VIDEO_TYPE_OMV",
    }
    state.update(tracks=[source, *([SONG] * 149), source], trackCount=151)
    item_id = run(service, imports.preview, owner, URL, shared)
    assert run(service, imports.refresh_one)
    if published:
        imports.confirm(owner, item_id)
    before = imports.get(owner, item_id)
    entry = before["entries"][0]
    assert entry["song_id"] is None and before["matched"] == 149
    candidate = entry["candidates"][0]
    provider_calls = []

    async def unexpected(payload):
        provider_calls.append(payload)
        raise AssertionError("Selection must not call the provider")

    imports.provider.fetch = unexpected
    token = browser(service)
    response = service[0].post(
        f"/playlist-imports/{item_id}/choose",
        data={
            "csrf": token,
            "source_key": entry["source_key"],
            "song_id": candidate["song_id"],
        },
    )
    assert response.status_code == 200
    assert "Confirmed" in response.text
    after = imports.get(owner, item_id)
    assert after["matched"] == 151
    for field in (
        "state",
        "phase",
        "ready",
        "processed",
        "total",
        "token",
        "attempts",
        "error",
        "next_attempt",
        "queued_at",
        "created",
    ):
        assert after[field] == before[field]
    assert after["entries"][1:-1] == before["entries"][1:-1]
    for position in (0, 150):
        assert after["entries"][position] == before["entries"][position] | {
            "song_id": candidate["song_id"],
            "reviewed": True,
            "reason": None,
        }
    assert after["progress"]["matches"] == before["progress"]["matches"]
    assert not run(service, imports.refresh_one)
    assert not provider_calls and len(calls) == 1
    if published:
        document = imports.document(owner, after, detail=True)
        assert document["songCount"] == 151
        assert document["entry"][0]["id"] == candidate["song_id"]
    reopened = make_accounts(service[1].state.services.database.path.parent)
    restored = ImportRepository(reopened.repository.database)
    assert restored.get(owner, item_id) == after


@pytest.mark.parametrize("paused", [False, True])
def test_partial_selection_preserves_progress_and_survives_old_worker(
    imported_service, paused
):
    service, imports, state, calls = imported_service
    owner = user(service)
    source = SONG | {
        "title": "Instant Crush <studios>",
        "videoType": "MUSIC_VIDEO_TYPE_OMV",
    }
    state.update(tracks=[source, *([SONG] * 24)], trackCount=25)

    async def scenario():
        entered, gate = asyncio.Event(), asyncio.Event()
        batch = imports.provider.batch

        async def waiting(progress):
            if len(progress["entries"]) == 10:
                entered.set()
                await gate.wait()
            return await batch(progress)

        imports.provider.batch = waiting
        item_id = await imports.preview(owner, URL, True)
        worker = asyncio.create_task(imports.refresh_one())
        await asyncio.wait_for(entered.wait(), 5)
        if paused:
            imports.control(owner, item_id, "pause")
            await asyncio.wait_for(worker, 5)
        before = imports.get(owner, item_id)
        progress = before["progress"]
        entry = progress["entries"][0]
        assert entry["song_id"] is None
        candidate = entry["candidates"][0]
        imports.decide(
            owner, item_id, entry["source_key"], candidate["song_id"]
        )
        selected = imports.get(owner, item_id)
        assert selected["matched"] == 10
        assert selected["progress"]["entries"][0]["reviewed"]
        assert selected["progress"]["tracks"] == progress["tracks"]
        assert selected["progress"]["entries"][1:] == progress["entries"][1:]
        for field in (
            "state",
            "phase",
            "processed",
            "total",
            "token",
            "next_attempt",
        ):
            assert selected[field] == before[field]
        if paused:
            assert not await imports.refresh_one()
            imports.control(owner, item_id, "resume")
            worker = asyncio.create_task(imports.refresh_one())
        gate.set()
        await asyncio.wait_for(worker, 5)
        complete = imports.get(owner, item_id)
        assert complete["state"] == "complete" and complete["matched"] == 25
        assert complete["entries"][0]["reviewed"]
        assert complete["entries"][0]["song_id"] == candidate["song_id"]
        assert len(calls) == 1

    run(service, scenario)


def test_selection_during_refresh_survives_old_final_publication(
    imported_service,
):
    service, imports, state, calls = imported_service
    owner = user(service)
    source = SONG | {
        "title": "Instant Crush <studios>",
        "videoType": "MUSIC_VIDEO_TYPE_OMV",
    }
    state.update(tracks=[source, SONG], trackCount=2)
    item_id = run(service, imports.preview, owner, URL, True)
    run(service, imports.refresh_one)
    imports.confirm(owner, item_id)
    job = imports.repository.claim(item_id)
    progress = run(service, imports.provider.prepare, job["source"], {})
    progress = run(service, imports.provider.batch, progress)
    assert imports.repository.checkpoint(job, progress)
    job["progress"] = copy.deepcopy(progress)
    entry = progress["entries"][0]
    candidate = entry["candidates"][0]
    before = imports.get(owner, item_id)
    imports.decide(owner, item_id, entry["source_key"], candidate["song_id"])
    selected = imports.get(owner, item_id)
    assert selected["token"] == before["token"]
    assert selected["state"] == "running"
    assert selected["matched"] == 2
    assert selected["entries"][0]["reviewed"]
    assert imports.repository.publish(
        job, Snapshot(progress["title"], progress["entries"])
    )
    final = imports.get(owner, item_id)
    assert final["state"] == "complete" and final["matched"] == 2
    assert final["entries"][0]["reviewed"]
    assert final["entries"][0]["song_id"] == candidate["song_id"]
    assert final["progress"]["decisions"][entry["source_key"]] == candidate
    assert len(calls) == 2
