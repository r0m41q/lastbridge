"""Per-import day intervals retain ownership and worker state."""

import time

import pytest
from support.imports import accept, accept_lastfm, browser, user
from support.library import run

from lastbridge.features.playlists.adapters.sqlite.imports import (
    ImportRepository,
)
from lastbridge.features.playlists.domain.imports import DAY
from lastbridge.shared.errors import CatalogError


def test_owner_sets_private_interval_in_report(lastfm_imported_service):
    service, imports, _, calls = lastfm_imported_service
    item_id = accept_lastfm(lastfm_imported_service)
    owner = user(service, "alice")
    before = imports.get(owner, item_id)
    token = browser(service, "alice")
    path = f"/playlist-imports/{item_id}/schedule"
    client = service[0]
    page = client.get(f"/playlist-imports/{item_id}")
    assert f'action="{path}"' in page.text
    assert 'name="days" min="1" max="36500"' in page.text
    assert 'value="1"' in page.text
    count = len(calls)
    for data, headers in (
        ({"days": "7"}, {}),
        ({"csrf": token, "days": "7"}, {"origin": "https://foreign.test"}),
    ):
        assert client.post(path, data=data, headers=headers).status_code == 403
    page = client.post(path, data={"csrf": token, "days": "7"})
    assert page.status_code == 200
    assert 'value="7"' in page.text
    assert "Refreshes every 7 day(s)" in page.text
    after = imports.get(owner, item_id)
    assert after["refresh_days"] == 7
    assert after["next_attempt"] == before["last_checked"] + 7 * DAY
    for key in ("updated", "last_checked", "entries", "state", "source"):
        assert after[key] == before[key]
    assert len(calls) == count
    # A newly constructed repository reads the durable saved schedule.
    reopened = ImportRepository(service[1].state.services.database)
    assert reopened.get(owner, item_id)["refresh_days"] == 7
    admin_token = browser(service)
    assert (
        client.post(path, data={"csrf": admin_token, "days": "2"}).status_code
        == 404
    )
    assert imports.get(owner, item_id)["refresh_days"] == 7


@pytest.mark.parametrize(
    "days", ["", "0", "-1", "1.5", "36501", "x", "١", "1" * 5000]
)
def test_private_interval_validation(lastfm_imported_service, days):
    service, imports, _, _ = lastfm_imported_service
    item_id = accept_lastfm(lastfm_imported_service)
    token = browser(service, "alice")
    response = service[0].post(
        f"/playlist-imports/{item_id}/schedule",
        data={"csrf": token, "days": days},
    )
    assert response.status_code == (413 if len(days) > 4096 else 400)
    assert imports.get(user(service, "alice"), item_id)["refresh_days"] == 1


def test_private_schedule_races_and_retry_deadlines(lastfm_imported_service):
    service, imports, state, _ = lastfm_imported_service
    item_id = accept_lastfm(lastfm_imported_service)
    owner = user(service, "alice")
    before = imports.get(owner, item_id)
    # The successful in-flight publication uses the newest interval.
    job = imports.repository.claim(item_id)
    leased = imports.get(owner, item_id)
    imports.schedule(owner, item_id, "3")
    scheduled = imports.get(owner, item_id)
    for key in ("token", "state", "phase", "next_attempt", "progress"):
        assert scheduled[key] == leased[key]
    assert run(service, imports.load, job)
    complete = imports.get(owner, item_id)
    assert complete["updated"] == before["updated"]
    assert complete["next_attempt"] == complete["last_checked"] + 3 * DAY
    assert imports.repository.claim() is None

    # Shortening an overdue interval makes the unchanged import eligible.
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE playlist_imports SET last_checked=? WHERE id=?",
            (time.time() - 2 * DAY, item_id),
        )
    imports.schedule(owner, item_id, "1")
    state["fail"] = True
    assert run(service, imports.refresh_one)
    failed = imports.get(owner, item_id)
    imports.schedule(owner, item_id, "5")
    scheduled = imports.get(owner, item_id)
    for key in ("state", "next_attempt", "updated", "last_checked", "entries"):
        assert scheduled[key] == failed[key]
    state["fail"] = False
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE playlist_imports SET next_attempt=0 WHERE id=?", (item_id,)
        )
    assert run(service, imports.refresh_one)
    current = imports.get(owner, item_id)
    assert current["next_attempt"] == current["last_checked"] + 5 * DAY


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_interval_edit_preserves_stopped_job(lastfm_imported_service, action):
    service, imports, _, _ = lastfm_imported_service
    item_id = accept_lastfm(lastfm_imported_service)
    owner = user(service, "alice")
    run(service, imports.refresh, owner, item_id)
    imports.control(owner, item_id, action)
    stopped = imports.get(owner, item_id)
    imports.schedule(owner, item_id, "4")
    after = imports.get(owner, item_id)
    for key in ("state", "next_attempt", "token", "progress", "entries"):
        assert after[key] == stopped[key]
    assert after["refresh_days"] == 4
    assert not run(service, imports.refresh_one)


def test_shared_report_interval_is_admin_only(imported_service):
    service, imports, _, _ = imported_service
    item_id = accept(imported_service, shared=True)
    client = service[0]
    path = f"/playlist-imports/{item_id}/schedule"
    token = browser(service)
    assert (
        f'action="{path}"' in client.get(f"/playlist-imports/{item_id}").text
    )
    assert (
        client.post(path, data={"csrf": token, "days": "6"}).status_code == 200
    )
    assert imports.get(user(service), item_id)["refresh_days"] == 6
    token = browser(service, "alice")
    assert (
        f'action="{path}"'
        not in client.get(f"/playlist-imports/{item_id}").text
    )
    assert (
        client.post(path, data={"csrf": token, "days": "2"}).status_code == 403
    )
    assert imports.get(user(service), item_id)["refresh_days"] == 6


def test_draft_and_manual_private_youtube_have_no_schedule(imported_service):
    service, imports, _, _ = imported_service
    private = accept(imported_service)
    token = browser(service)
    assert (
        "Save interval"
        not in service[0].get(f"/playlist-imports/{private}").text
    )
    with pytest.raises(CatalogError, match="manually"):
        imports.schedule(user(service), private, "2")
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='linked-user' WHERE username='admin'"
        )
    draft = run(service, imports.preview, user(service), "12354876")
    response = service[0].post(
        f"/playlist-imports/{draft}/schedule",
        data={"csrf": token, "days": "2"},
    )
    assert response.status_code == 400
    assert "Confirm the preview first" in response.text
