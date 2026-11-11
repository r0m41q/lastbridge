import asyncio
import time

import pytest
from support.build_accounts import make_accounts
from support.imports import (
    URL,
    ChartLookup,
    accept,
    chart_tracks,
    due,
    user,
)
from support.library import run

from lastbridge.features.playlists.adapters.sqlite.imports import (
    ImportRepository,
)
from lastbridge.features.playlists.domain.imports import Snapshot
from lastbridge.shared.errors import CatalogError


def test_large_preview_is_queued_deduplicated_ordered_and_metadata_only(
    imported_service,
):
    service, imports, state, calls = imported_service
    owner = user(service)
    state.update(tracks=chart_tracks(151), trackCount=151)
    lookup = ChartLookup(imports.catalog)
    imports.provider.match = lookup
    item_id = run(service, imports.preview, owner, URL, True)
    assert run(service, imports.preview, owner, URL, True) == item_id
    pending = imports.get(owner, item_id)
    assert pending["state"] == "queued" and pending["processed"] == 0
    assert calls == []
    with pytest.raises(CatalogError, match="unfinished"):
        imports.confirm(owner, item_id)
    assert imports.rows(owner) == []
    assert run(service, imports.refresh_one)
    preview = imports.get(owner, item_id)
    assert preview["state"] == "complete" and not preview["ready"]
    assert preview["processed"] == preview["total"] == 151
    assert preview["matched"] == 151
    assert [entry["title"] for entry in preview["entries"]] == [
        f"Track {index}" for index in range(151)
    ]
    assert lookup.maximum == 2 and lookup.active == 0
    assert len(lookup.calls) == 151 and len(calls) == 1
    assert not run(service, imports.refresh_one)
    imports.confirm(owner, item_id)
    # Reuse recent exact matches on a fresh snapshot, including duplicates.
    state["tracks"] += state["tracks"][:1]
    state["trackCount"] += 1
    run(service, imports.refresh, owner, item_id)
    run(service, imports.refresh, owner, item_id)
    assert run(service, imports.refresh_one)
    refreshed = imports.get(owner, item_id)
    assert refreshed["total"] == 152
    assert len(lookup.calls) == 151 and len(calls) == 2
    assert (
        refreshed["entries"][0]["song_id"]
        == (refreshed["entries"][-1]["song_id"])
    )
    with service[1].state.services.database.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM save_jobs").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("stop", ["pause", "cancel", "shutdown", "crash"])
def test_completed_batches_survive_controls_and_restart_without_refetch(
    imported_service,
    stop,
):
    service, imports, state, calls = imported_service
    owner = user(service)
    state.update(tracks=chart_tracks(23), trackCount=23)

    async def scenario():
        lookup = ChartLookup(imports.catalog, block_from=10)
        imports.provider.match = lookup
        item_id = await imports.preview(owner, URL, True)
        worker = asyncio.create_task(imports.refresh_one())
        await asyncio.wait_for(lookup.entered.wait(), 5)
        saved = imports.get(owner, item_id)
        assert saved["processed"] == 10 and saved["matched"] == 10
        stale = saved | {"token": saved["token"]}
        if stop in ("pause", "cancel"):
            imports.control(owner, item_id, stop)
            await asyncio.wait_for(worker, 5)
        else:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
        if stop == "crash":
            with service[1].state.services.database.transaction() as conn:
                conn.execute(
                    "UPDATE playlist_imports SET state='running',"
                    "token='abandoned' WHERE id=?",
                    (item_id,),
                )
        # Reopen SQLite through the normal startup migration, not the old
        # repository object. No in-memory provider cursor is reused.
        accounts = make_accounts(
            service[1].state.services.database.path.parent
        )
        repository = ImportRepository(accounts.repository.database)
        repository.recover()
        restored = repository.get(owner, item_id)
        assert restored["processed"] == 10
        assert restored["progress"] == saved["progress"]
        assert restored["token"] is None
        if stop in ("pause", "cancel"):
            assert repository.claim() is None
            repository.control(
                owner, item_id, "resume" if stop == "pause" else "retry"
            )
        # A late pre-control result is fenced out, including on resume.
        assert not repository.publish(stale, Snapshot("Wrong", []))
        restarted = type(imports)(
            repository, imports.provider, imports.catalog, imports.library
        )
        lookup.gate.set()
        assert await restarted.refresh_one()
        result = repository.get(owner, item_id)
        assert result["state"] == "complete" and result["total"] == 23
        assert result["matched"] == 23
        assert lookup.active == 0
        assert all(lookup.calls.count(f"Track {i}") == 1 for i in range(10))
        assert len(calls) == 1

    run(service, scenario)


def test_batch_failure_retries_three_times_then_manual_retry_resumes(
    imported_service,
):
    service, imports, state, calls = imported_service
    owner = user(service)
    state.update(tracks=chart_tracks(25), trackCount=25)
    lookup = ChartLookup(imports.catalog)
    lookup.error = CatalogError()
    imports.provider.match = lookup
    item_id = run(service, imports.preview, owner, URL)
    for attempt in range(1, 4):
        due(service, item_id)
        assert run(service, imports.refresh_one)
        failed = imports.get(owner, item_id)
        assert failed["attempts"] == attempt
        assert failed["processed"] == 10
        assert failed["state"] == ("failed" if attempt == 3 else "queued")
        assert failed["error"].startswith("resolve:")
        assert failed["next_attempt"] >= time.time() + 55
    due(service, item_id)
    assert not run(service, imports.refresh_one)
    lookup.error = None
    imports.control(owner, item_id, "retry")
    assert run(service, imports.refresh_one)
    completed = imports.get(owner, item_id)
    assert completed["state"] == "complete" and completed["matched"] == 25
    assert completed["error"] is None and completed["attempts"] == 0
    assert all(lookup.calls.count(f"Track {i}") == 1 for i in range(10))
    assert len(calls) == 1


def test_lookup_deadline_releases_slots_and_preserves_checkpoint(
    imported_service,
    monkeypatch,
):
    service, imports, state, _ = imported_service
    owner = user(service)
    state.update(tracks=chart_tracks(20), trackCount=20)
    lookup = ChartLookup(imports.catalog, block_from=10)
    imports.provider.match = lookup
    monkeypatch.setattr(
        "lastbridge.features.playlists.adapters.youtube.imports."
        "LOOKUP_TIMEOUT",
        0.02,
    )
    item_id = run(service, imports.preview, owner, URL)
    assert run(service, imports.refresh_one)
    row = imports.get(owner, item_id)
    assert row["processed"] == 10
    assert row["error"] == "resolve: Provider request timed out"
    assert row["state"] == "queued" and lookup.active == 0
    lookup.gate.set()
    due(service, item_id)
    assert run(service, imports.refresh_one)
    assert imports.get(owner, item_id)["state"] == "complete"


def test_cancelled_refresh_preserves_published_snapshot_and_custom_title(
    imported_service,
):
    service, imports, state, _ = imported_service
    owner = user(service)
    item_id = accept(imported_service, shared=True)
    imports.rename_shared(owner, item_id, "My chart")
    before = imports.get(owner, item_id)
    state.update(tracks=chart_tracks(20), trackCount=20)

    async def scenario():
        lookup = ChartLookup(imports.catalog, block_from=10)
        imports.provider.match = lookup
        await imports.refresh(owner, item_id)
        worker = asyncio.create_task(imports.refresh_one())
        await asyncio.wait_for(lookup.entered.wait(), 5)
        during = imports.get(owner, item_id)
        assert during["entries"] == before["entries"]
        assert during["title"] == "My chart"
        imports.control(owner, item_id, "cancel")
        await worker
        assert not await imports.refresh_one()
        imports.control(owner, item_id, "retry")
        lookup.gate.set()
        assert await imports.refresh_one()
        after = imports.get(owner, item_id)
        assert after["title"] == "My chart"
        assert len(after["entries"]) == 20

    run(service, scenario)


def test_deleted_running_job_cannot_reappear(imported_service):
    service, imports, state, _ = imported_service
    owner = user(service)
    state.update(tracks=chart_tracks(20), trackCount=20)

    async def scenario():
        lookup = ChartLookup(imports.catalog, block_from=10)
        imports.provider.match = lookup
        item_id = await imports.preview(owner, URL, True)
        worker = asyncio.create_task(imports.refresh_one())
        await asyncio.wait_for(lookup.entered.wait(), 5)
        stale = imports.get(owner, item_id)
        imports.remove(owner, item_id)
        await worker
        assert not imports.repository.publish(stale, Snapshot("Late", []))
        assert imports.queue(owner) == []

    run(service, scenario)


def test_permanent_payload_failure_stops_until_retry(imported_service):
    service, imports, state, calls = imported_service
    owner = user(service)
    state.update(tracks=chart_tracks(1001), trackCount=1001)
    item_id = run(service, imports.preview, owner, URL)
    assert run(service, imports.refresh_one)
    failed = imports.get(owner, item_id)
    assert failed["state"] == "failed" and failed["phase"] == "fetch"
    assert "exceeds 1000" in failed["error"]
    due(service, item_id)
    assert not run(service, imports.refresh_one)
    assert len(calls) == 1
    state.update(tracks=chart_tracks(1), trackCount=1)
    imports.provider.match = ChartLookup(imports.catalog)
    imports.control(owner, item_id, "retry")
    assert run(service, imports.refresh_one)
    assert imports.get(owner, item_id)["matched"] == 1


def test_background_worker_wakes_for_new_jobs_and_survives_pause(
    imported_service,
):
    service, imports, state, _ = imported_service
    owner = user(service)
    state.update(tracks=chart_tracks(20), trackCount=20)

    async def scenario():
        lookup = ChartLookup(imports.catalog, block_from=10)
        imports.provider.match = lookup
        await imports.start()
        try:
            item_id = await imports.preview(owner, URL, True)
            await asyncio.wait_for(lookup.entered.wait(), 5)
            imports.control(owner, item_id, "pause")
            while imports.active:
                await asyncio.sleep(0.01)
            assert not imports.task.done()
            assert imports.get(owner, item_id)["state"] == "paused"
            imports.control(owner, item_id, "resume")
            lookup.gate.set()

            async def completed():
                while imports.get(owner, item_id)["state"] != "complete":
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(completed(), 5)
            assert not imports.task.done()
        finally:
            await imports.close()
        assert imports.task is None and imports.active is None
        assert lookup.active == 0

    run(service, scenario)


def test_preview_expiry_starts_after_completion_not_queue_submission(
    imported_service,
):
    service, imports, _, _ = imported_service
    owner = user(service)
    item_id = run(service, imports.preview, owner, URL)
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE playlist_imports SET created=0,updated=0 WHERE id=?",
            (item_id,),
        )
    assert imports.get(owner, item_id)["state"] == "queued"
    assert run(service, imports.refresh_one)
    assert imports.confirm(owner, item_id) == item_id


def test_expired_lease_can_be_reclaimed_and_old_results_are_fenced(
    imported_service,
):
    service, imports, _, _ = imported_service
    owner = user(service)
    item_id = run(service, imports.preview, owner, URL, True)
    first = imports.repository.claim()
    assert imports.repository.claim() is None
    due(service, item_id)
    second = imports.repository.claim()
    assert first["token"] != second["token"]
    assert not imports.repository.publish(first, Snapshot("Old", []))
    assert run(service, imports.load, second)
    assert imports.get(owner, item_id)["state"] == "complete"
