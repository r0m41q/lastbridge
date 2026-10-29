import time
from types import SimpleNamespace

import pytest
import test_sync
from pydantic import ValidationError
from test_accounts import csrf, login
from test_catalog import body, get, search
from test_sync import rows, run, uid

import lastbridge.mixes as mix_module
from lastbridge.accounts import Accounts
from lastbridge.catalog import CatalogError
from lastbridge.config import Settings

sync_service = test_sync.sync_service
DAY = 86400


@pytest.fixture
def scheduled_service(sync_service, monkeypatch):
    service = sync_service
    mixes = service[1].state.mixes
    clock = [time.time()]
    monkeypatch.setattr(
        mix_module, "time", SimpleNamespace(time=lambda: clock[0])
    )
    mixes.linked(uid(service))
    calls = []

    async def build(job):
        calls.append(job["user_id"])
        return {"generation": len(calls)}, {}

    monkeypatch.setattr(mixes, "build", build)
    return service, mixes, clock, calls


def test_manual_success_resets_interval_and_no_duplicate_due_jobs(
    scheduled_service,
):
    service, mixes, clock, calls = scheduled_service
    user_id = uid(service)
    assert run(service, mixes.refresh_one)
    first_due = clock[0] + DAY
    assert mixes.status(user_id)["next_attempt"] == first_due
    clock[0] += 6 * 3600
    mixes.request(user_id)
    assert run(service, mixes.refresh_one)
    new_due = clock[0] + DAY
    assert mixes.status(user_id)["next_attempt"] == new_due
    clock[0] = first_due
    assert run(service, mixes.refresh_one) is False
    clock[0] = new_due
    assert run(service, mixes.refresh_one)
    assert run(service, mixes.refresh_one) is False
    assert calls == [user_id] * 3


def test_inactivity_boundary_and_returning_search(scheduled_service):
    service, mixes, clock, calls = scheduled_service
    assert run(service, mixes.refresh_one)
    previous = mixes.status(uid(service))
    clock[0] += 7 * DAY
    assert run(service, mixes.refresh_one) is False
    assert mixes.status(uid(service))["snapshot"] == previous["snapshot"]
    assert len(calls) == 1
    assert body(get(service, "ping"))["status"] == "ok"
    assert run(service, mixes.refresh_one) is False
    search(service)
    assert mixes.status(uid(service))["last_active"] == clock[0]
    assert run(service, mixes.refresh_one)
    assert run(service, mixes.refresh_one) is False
    assert len(calls) == 2


def test_idle_retry_stops_and_resume_keeps_backoff(
    scheduled_service, monkeypatch
):
    service, mixes, clock, _ = scheduled_service
    assert run(service, mixes.refresh_one)
    previous = mixes.status(uid(service))
    clock[0] += DAY

    async def failed(job):
        raise CatalogError()

    monkeypatch.setattr(mixes, "build", failed)
    assert run(service, mixes.refresh_one)
    status = mixes.status(uid(service))
    assert status["snapshot"] == previous["snapshot"]
    assert status["updated"] == previous["updated"]
    assert status["next_attempt"] == clock[0] + 300
    clock[0] += 7 * DAY
    assert run(service, mixes.refresh_one) is False
    mixes.touch(uid(service))
    assert run(service, mixes.refresh_one)
    assert run(service, mixes.refresh_one) is False


def test_polling_head_failed_auth_and_bad_search_do_not_extend_activity(
    scheduled_service,
):
    service, mixes, clock, _ = scheduled_service
    old = mixes.status(uid(service))["last_active"]
    clock[0] += 8 * DAY
    for endpoint, params in (
        ("ping", {}),
        ("getPlaylists", {}),
        ("getArtists", {}),
        ("getAlbumList2", {"type": "newest"}),
        ("search3", {"query": ""}),
        ("search3", {"query": '""'}),
        ("search3", {"query": "test", "apiKey": "invalid"}),
        ("search3", {"query": "test", "songCount": "-1"}),
    ):
        get(service, endpoint, **params)
    service[0].head(
        "/rest/search3",
        params={
            "apiKey": service[3]["admin"],
            "query": "test",
            "c": "test",
            "v": "1.16.1",
            "f": "json",
        },
    )
    assert mixes.status(uid(service))["last_active"] == old


def test_activity_is_private_throttled_and_survives_restart(scheduled_service):
    service, mixes, clock, _ = scheduled_service
    mixes.linked(uid(service, "alice"))
    old = mixes.status(uid(service))["last_active"]
    clock[0] += 30
    mixes.touch(uid(service))
    assert mixes.status(uid(service))["last_active"] == old
    clock[0] += 31
    mixes.touch(uid(service, "alice"))
    assert mixes.status(uid(service))["last_active"] == old
    assert mixes.status(uid(service, "alice"))["last_active"] == clock[0]
    store = Accounts(service[1].state.accounts.path.parent)
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT last_active FROM mix_activity WHERE user_id=?",
                (uid(service, "alice"),),
            ).fetchone()[0]
            == clock[0]
        )


def test_start_does_not_reactivate_idle_accounts(scheduled_service):
    service, mixes, clock, calls = scheduled_service
    mixes.linked(uid(service, "alice"))
    assert run(service, mixes.refresh_one)
    clock[0] += 8 * DAY
    old_active = mixes.status(uid(service))["last_active"]

    async def scenario():
        await mixes.start()
        assert mixes.status(uid(service))["last_active"] == old_active
        assert await mixes.refresh_one() is False
        await mixes.close()

    run(service, scenario)
    assert len(calls) == 1


def test_manual_browser_refresh_reactivates_and_shows_schedule(
    scheduled_service,
):
    service, mixes, clock, _ = scheduled_service
    clock[0] += 8 * DAY
    login(service[0])
    view = service[0].get("/mixes")
    assert "every 24 hours" in view.text
    assert "7 days without activity" in view.text
    assert mixes.status(uid(service))["last_active"] == clock[0]
    assert (
        service[0]
        .post(
            "/mixes/refresh", data={"csrf": csrf(view)}, follow_redirects=False
        )
        .status_code
        == 303
    )
    assert run(service, mixes.refresh_one)
    assert (
        "Next refresh or retry eligible at:" in service[0].get("/mixes").text
    )


def test_v8_upgrade_preserves_snapshots_and_waits_for_activity(
    scheduled_service,
):
    service, mixes, clock, _ = scheduled_service
    assert run(service, mixes.refresh_one)
    before = rows(service, "mixes")
    with service[1].state.accounts.transaction() as connection:
        connection.execute("DROP TABLE mix_activity")
        connection.execute("PRAGMA user_version=8")
    store = Accounts(service[1].state.accounts.path.parent)
    assert rows(service, "mixes") == before
    assert store.client_user({"apiKey": service[3]["admin"]})
    assert not rows(service, "mix_activity")
    clock[0] += 2 * DAY
    assert run(service, mixes.refresh_one) is False
    mixes.linked(uid(service))
    assert not rows(service, "mix_activity")
    mixes.touch(uid(service))
    assert run(service, mixes.refresh_one)
    with store.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9


def test_configured_interval_changes_on_restart_without_erasing_manual_work(
    scheduled_service,
):
    service, mixes, clock, _ = scheduled_service
    assert run(service, mixes.refresh_one)
    mixes.refresh_interval = 48 * 3600

    async def scenario():
        await mixes.start()
        assert mixes.status(uid(service))["next_attempt"] == clock[0] + 2 * DAY
        await mixes.close()
        mixes.request(uid(service))
        await mixes.start()
        assert mixes.status(uid(service))["next_attempt"] == 0
        await mixes.close()

    run(service, scenario)


@pytest.mark.parametrize("field", ["mix_refresh_hours", "mix_inactive_days"])
@pytest.mark.parametrize("value", [0, -1, "invalid", 100000])
def test_invalid_refresh_settings_are_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_refresh_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("LASTBRIDGE_MIX_REFRESH_HOURS", "48")
    monkeypatch.setenv("LASTBRIDGE_MIX_INACTIVE_DAYS", "14")
    settings = Settings(_env_file=None)
    assert settings.mix_refresh_hours == 48
    assert settings.mix_inactive_days == 14
