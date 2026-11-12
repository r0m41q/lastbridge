import asyncio
from datetime import UTC, datetime, timedelta
from xml.etree.ElementTree import fromstring

import pytest
from support.build_accounts import make_accounts
from support.catalog import body, get, search

from lastbridge.features.playback.adapters.sqlite.queue import QueueRepository
from lastbridge.features.playback.application.queue import PlayQueue
from lastbridge.shared.errors import StorageError


def songs(service):
    album = search(service)["album"][0]["id"]
    return body(get(service, "getAlbum", id=album))["album"]["song"]


def test_save_restore_clear_and_user_isolation(catalog_service):
    client, app, _, keys = catalog_service
    tracks = songs(catalog_service)
    ids = [song["id"] for song in tracks]
    assert body(get(catalog_service, "getPlayQueue"))["playQueue"] == {
        "entry": []
    }
    response = get(
        catalog_service,
        "savePlayQueue",
        id=ids,
        current=ids[0],
        position="12345",
    )
    assert body(response)["status"] == "ok"
    queue = body(get(catalog_service, "getPlayQueue"))["playQueue"]
    assert [entry["id"] for entry in queue["entry"]] == ids
    assert queue["current"] == ids[0] and queue["position"] == 12345
    assert queue["username"] == "admin" and queue["changedBy"] == "test"
    assert "changed" in queue
    assert body(get(catalog_service, "getPlayQueue", apiKey=keys["alice"]))[
        "playQueue"
    ] == {"entry": []}
    # Queue persistence does not depend on the service instance or workers.
    services = app.state.services
    restarted = PlayQueue(
        QueueRepository(services.database), services.catalog, services.audio
    )
    assert restarted.get(services.accounts.user("admin")["id"]) == queue
    xml = get(catalog_service, "getPlayQueue", f="xml")
    assert len(fromstring(xml.content).findall(".//{*}entry")) == len(ids)
    get(catalog_service, "savePlayQueue")
    cleared = body(get(catalog_service, "getPlayQueue"))["playQueue"]
    assert cleared["entry"] == [] and "current" not in cleared
    assert cleared["position"] == 0


@pytest.mark.parametrize(
    "invalid",
    [
        {"current": "missing"},
        {"position": "-1"},
        {"position": "bad"},
        {"id": ["missing"]},
        {"current": ""},
        {"id": []},
    ],
)
def test_invalid_queue_preserves_previous_snapshot(catalog_service, invalid):
    track = songs(catalog_service)[0]["id"]
    valid = {"id": [track], "current": track}
    get(catalog_service, "savePlayQueue", **valid)
    before = body(get(catalog_service, "getPlayQueue"))["playQueue"]
    response = get(catalog_service, "savePlayQueue", **(valid | invalid))
    assert body(response)["status"] == "failed"
    assert body(get(catalog_service, "getPlayQueue"))["playQueue"] == before


def test_post_bounds_head_and_prefetch_without_play_history(catalog_service):
    client, app, _, keys = catalog_service
    tracks = songs(catalog_service)
    ids = [song["id"] for song in tracks]
    params = {
        "apiKey": keys["admin"],
        "v": "1.16.1",
        "c": "test",
        "f": "json",
        "current": ids[0],
        "id": ids,
        "position": "",
    }
    response = client.post("/rest/savePlayQueue", data=params)
    assert body(response)["status"] == "ok"

    async def finished():
        await asyncio.gather(
            *list(app.state.services.play_queue.tasks.values())
        )

    client.portal.call(finished)
    audio = app.state.services.audio
    assert audio.provider.calls == ["othertrack1"]
    assert not audio.storage.leases
    assert all(not entry.get("saved") for entry in audio.entries.values())
    user_id = app.state.services.accounts.user("admin")["id"]
    assert not app.state.services.library.repository.plays(user_id)
    before = body(get(catalog_service, "getPlayQueue"))["playQueue"]
    client.head("/rest/savePlayQueue", params=params)
    assert body(get(catalog_service, "getPlayQueue"))["playQueue"] == before
    response = client.post(
        "/rest/savePlayQueue", data=params | {"id": [ids[0]] * 1001}
    )
    assert body(response)["error"]["code"] == 10
    assert body(get(catalog_service, "getPlayQueue"))["playQueue"] == before
    assert (
        body(get(catalog_service, "savePlayQueue", apiKey=keys["unlinked"]))[
            "error"
        ]["code"]
        == 50
    )


def test_stale_other_client_and_ambiguous_queues_do_not_prefetch(
    catalog_service,
):
    client, app, _, _ = catalog_service
    tracks = songs(catalog_service)
    ids = [song["id"] for song in tracks]
    service = app.state.services.play_queue
    user_id = app.state.services.accounts.user("admin")["id"]
    document = {
        "ids": ids,
        "changedBy": "test",
        "changed": datetime.now(UTC).isoformat(),
    }

    async def scenario():
        for update, client_name in (
            ({}, "another-client"),
            (
                {
                    "changed": (
                        datetime.now(UTC) - timedelta(hours=2)
                    ).isoformat()
                },
                "test",
            ),
            ({"ids": [ids[0], ids[0], ids[1]]}, "test"),
        ):
            service.repository.save(user_id, document | update)
            service.playing(user_id, client_name, ids[0])
            assert not service.tasks

    client.portal.call(scenario)
    assert not app.state.services.audio.provider.calls


def test_queue_storage_errors_are_redacted(catalog_service, monkeypatch):
    _, app, _, _ = catalog_service

    def failure(*args):
        raise StorageError("secret filesystem location")

    monkeypatch.setattr(
        app.state.services.play_queue.repository, "get", failure
    )
    response = get(catalog_service, "getPlayQueue")
    assert body(response)["error"]["code"] == 0
    assert "secret" not in response.text


def test_version_eighteen_upgrade_creates_private_queues(tmp_path):
    accounts = make_accounts(tmp_path)
    with accounts.repository.database.transaction() as connection:
        connection.execute("DROP TABLE play_queues")
        connection.execute("PRAGMA user_version=18")
    accounts = make_accounts(tmp_path)
    accounts.create_user("test", "test-password", bootstrap=True)
    user_id = accounts.user("test")["id"]
    repository = QueueRepository(accounts.repository.database)
    repository.save(user_id, {"ids": [], "position": 0})
    assert repository.get(user_id) == {"ids": [], "position": 0}
    with accounts.repository.database.transaction() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20
        connection.execute("DELETE FROM users WHERE id=?", (user_id,))
    assert repository.get(user_id) is None
