from support.audio import AUDIO
from support.catalog import SONG, body, get, search
from support.library import complete_jobs, run

from lastbridge.features.administration.domain.accounting import (
    account_storage,
)


def test_accounting_keeps_logical_sizes_and_deduplicates_physical_bytes():
    users = [{"id": 1}, {"id": 2}, {"id": 3}]
    retained = {1: {"shared", "exclusive", "pending"}, 2: {"shared"}}
    entries = {
        "shared-aac": {"size": 100, "saved": True},
        "exclusive-aac": {"size": 200, "saved": True},
        "cache-aac": {"size": 50},
        "old-opus": {"size": 75, "saved": True},
    }
    totals = account_storage(users, retained, entries, "aac")
    assert totals == {
        "saved_bytes": 375,
        "cache_bytes": 50,
        "shared_bytes": 100,
    }
    assert [u["logical_bytes"] for u in users] == [300, 100, 0]
    assert [u["exclusive_bytes"] for u in users] == [200, 0, 0]
    assert [u["retained"] for u in users] == [3, 1, 0]
    assert [u["downloaded"] for u in users] == [2, 1, 0]


def test_two_users_share_one_physical_file_and_release_independently(
    saved_service,
):
    service = saved_service
    _, app, _, keys = service
    song = search(service)["song"][0]["id"]
    for name in ("admin", "alice"):
        get(service, "star", id=song, apiKey=keys[name])
    complete_jobs(service)
    admin = app.state.services.administration
    report = run(service, admin.dashboard)
    assert report["storage"]["saved_bytes"] == len(AUDIO)
    assert report["storage"]["shared_bytes"] == len(AUDIO)
    assert report["job_counts"] == {"complete": 1}
    assert [u["logical_bytes"] for u in report["users"]] == [
        len(AUDIO),
        len(AUDIO),
        0,
    ]
    assert [u["exclusive_bytes"] for u in report["users"]] == [0, 0, 0]
    assert len(app.state.services.audio.provider.calls) == 1
    get(service, "unstar", id=song, apiKey=keys["alice"])
    report = run(service, admin.dashboard)
    assert report["storage"]["saved_bytes"] == len(AUDIO)
    assert report["storage"]["shared_bytes"] == 0
    assert report["users"][0]["exclusive_bytes"] == len(AUDIO)
    assert report["users"][1]["logical_bytes"] == 0
    get(service, "unstar", id=song)
    report = run(service, admin.dashboard)
    assert report["storage"]["saved_bytes"] == 0
    assert report["storage"]["cache_bytes"] == len(AUDIO)


def test_unsaved_streams_and_pending_retention_are_not_saved_bytes(
    saved_service,
):
    service = saved_service
    song = search(service)["song"][0]["id"]
    assert get(service, "stream", id=song).content == AUDIO
    another = service[1].state.services.catalog.song(
        SONG | {"videoId": "othertrack1"}
    )["id"]
    assert body(get(service, "star", id=another))["status"] == "ok"
    admin = service[1].state.services.administration
    report = run(service, admin.dashboard)
    assert report["storage"]["cache_bytes"] == len(AUDIO)
    assert report["storage"]["saved_bytes"] == 0
    assert report["users"][0]["retained"] == 1
    assert report["users"][0]["downloaded"] == 0
    assert report["users"][0]["logical_bytes"] == 0


def test_liked_album_song_and_kept_playlist_do_not_multiply_user_usage(
    saved_service,
):
    service = saved_service
    result = search(service)
    song = result["song"][0]["id"]
    album = result["album"][0]["id"]
    get(service, "star", id=album)
    get(service, "star", id=song)
    account = service[1].state.services.accounts.user("admin")
    playlists = service[1].state.services.playlists
    playlist = run(service, playlists.create, account, "Keep", [song, song])
    run(service, playlists.keep, account, playlist["id"], True)
    complete_jobs(service)
    report = run(service, service[1].state.services.administration.dashboard)
    assert report["users"][0]["retained"] == 2
    assert report["users"][0]["logical_bytes"] == 2 * len(AUDIO)
    assert report["storage"]["saved_bytes"] == 2 * len(AUDIO)
