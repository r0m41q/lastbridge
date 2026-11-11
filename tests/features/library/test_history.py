from datetime import UTC, datetime
from urllib.parse import urlencode
from xml.etree.ElementTree import fromstring

import pytest
from support.build_accounts import make_accounts
from support.build_catalog import make_catalog
from support.build_library import make_library
from support.catalog import body, get
from support.history import album_list, submit
from support.library import complete_jobs, user_id


def test_album_counts_track_submissions_use_latest_time_and_dedupe(
    history_service,
):
    case = history_service
    service = case.service
    catalog = service[1].state.services.catalog
    for song, offset in ((0, 300), (1, 100), (0, 200), (0, 300)):
        submit(case, song, offset)
    catalog.provider.fail = True
    album = body(get(service, "getAlbum", id=case.albums[0]["id"]))["album"]
    assert album["playCount"] == 3
    assert datetime.fromisoformat(album["played"]).timestamp() == (
        case.stamp + 300
    )
    assert album["created"] == case.albums[0]["created"]
    assert [s["playCount"] for s in album["song"]] == [2, 1]
    artist = body(get(service, "getArtist", id=case.artist["id"]))["artist"]
    assert artist["album"][0]["playCount"] == 3
    assert all(
        "played" not in a and "playCount" not in a for a in artist["album"][1:]
    )
    directory = body(get(service, "getMusicDirectory", id=case.artist["id"]))[
        "directory"
    ]
    assert directory["child"][0]["played"] == album["played"]
    assert directory["child"][0]["playCount"] == 3
    assert "played" not in catalog.read(album["id"])["document"]
    assert "playCount" not in catalog.read(album["song"][0]["id"])["document"]
    # Hearing an album does not save it or its songs into the personal library.
    assert body(get(service, "search3", query=""))["searchResult3"] == {
        "artist": [],
        "album": [],
        "song": [],
    }
    assert album_list(case, "newest") == []


@pytest.mark.parametrize("method", ["getAlbumList", "getAlbumList2"])
def test_recent_frequent_order_stable_paging_and_unsaved_albums(
    history_service,
    method,
):
    case = history_service
    for offset in (10, 20, 30):
        submit(case, 0, offset)
    submit(case, 2, 40)
    a, b, c = [row["id"] for row in case.albums]
    recent = album_list(case, method=method)
    frequent = album_list(case, "frequent", method=method)
    assert [row["id"] for row in recent] == [b, a]
    assert [row["id"] for row in frequent] == [a, b]
    assert c not in {row["id"] for row in recent}
    assert "song" not in recent[0] and recent[0]["songCount"] == 1
    if method == "getAlbumList":
        assert recent[0]["isDir"] is True
        assert recent[0]["title"] == case.albums[1]["name"]
    submit(case, 2, 50)
    submit(case, 2, 60)
    for offset in (10, 20, 60):
        submit(case, 3, offset)
    expected = sorted([b, c]) + [a]
    for kind in ("recent", "frequent"):
        assert [
            row["id"] for row in album_list(case, kind, method=method)
        ] == (expected)
        assert [
            row["id"]
            for page in range(3)
            for row in album_list(
                case, kind, method=method, size=1, offset=page
            )
        ] == expected
        assert album_list(case, kind, method=method, size=1, offset=3) == []
        assert album_list(case, kind, method=method, size=0) == []


def test_album_histories_and_favorites_are_private_per_user(history_service):
    case = history_service
    service = case.service
    submit(case, 0, 10)
    submit(case, 1, 20)
    submit(case, 2, 30, user="alice")
    a, b = case.albums[:2]
    assert album_list(case)[0]["id"] == a["id"]
    assert album_list(case)[0]["playCount"] == 2
    assert album_list(case, user="alice")[0]["id"] == b["id"]
    assert (
        "played"
        not in body(
            get(service, "getAlbum", id=a["id"], apiKey=service[3]["alice"])
        )["album"]
    )
    assert "played" not in body(get(service, "getAlbum", id=b["id"]))["album"]
    get(service, "star", albumId=a["id"])
    assert album_list(case)[0]["starred"]
    assert (
        "starred"
        not in body(
            get(service, "getAlbum", id=a["id"], apiKey=service[3]["alice"])
        )["album"]
    )
    assert (
        body(
            get(
                service,
                "getAlbumList2",
                type="recent",
                apiKey=service[3]["unlinked"],
            )
        )["error"]["code"]
        == 50
    )


@pytest.mark.parametrize(
    "state",
    [
        "pending",
        "running",
        "sent",
        "ignored",
        "failed",
        "blocked",
        "uncertain",
    ],
)
def test_local_history_is_independent_of_lastfm_delivery(
    history_service, state
):
    case = history_service
    submit(case, 0, 10)
    with case.service[1].state.services.database.transaction() as connection:
        connection.execute("UPDATE scrobbles SET state=?", (state,))
    assert album_list(case)[0]["playCount"] == 1
    song = body(get(case.service, "getSong", id=case.songs[0]["id"]))["song"]
    assert song["playCount"] == 1


@pytest.mark.parametrize("method", ["search2", "search3"])
def test_library_sync_exposes_real_stats_without_adding_heard_tracks(
    history_service,
    method,
):
    case = history_service
    service = case.service
    submit(case, 0, 10)
    submit(case, 0, 20)
    submit(case, 1, 30)
    submit(case, 2, 40)
    get(service, "star", id=case.songs[0]["id"])
    synced = body(get(service, method, query="", songCount=500))[
        "searchResult2" if method == "search2" else "searchResult3"
    ]
    assert [s["id"] for s in synced["song"]] == [case.songs[0]["id"]]
    assert synced["song"][0]["playCount"] == 2
    assert len(synced["album"]) == 1
    assert synced["album"][0]["id"] == case.albums[0]["id"]
    assert synced["album"][0]["songCount"] == 1
    assert synced["album"][0]["playCount"] == 3
    assert len(album_list(case)) == 2
    assert (
        body(get(service, "getAlbumList2", type="alphabeticalByName"))[
            "albumList2"
        ]["album"][0]["playCount"]
        == 3
    )
    get(service, "unstar", id=case.songs[0]["id"])
    assert (
        body(get(service, method, query=""))[
            "searchResult2" if method == "search2" else "searchResult3"
        ]["song"]
        == []
    )
    assert len(album_list(case)) == 2


@pytest.mark.parametrize(
    "method,field",
    [
        ("getStarred", "starred"),
        ("getStarred2", "starred2"),
    ],
)
def test_starred_albums_and_direct_collection_include_history(
    history_service,
    method,
    field,
):
    case = history_service
    service = case.service
    submit(case, 0, 10)
    get(service, "star", albumId=case.albums[0]["id"])
    starred = body(get(service, method))[field]["album"][0]
    assert starred["playCount"] == 1
    assert datetime.fromisoformat(starred["played"]).timestamp() == (
        case.stamp + 10
    )
    collection = service[1].state.services.library.collection(user_id(service))
    assert collection["album"][0]["playCount"] == 1
    assert collection["song"][0]["playCount"] == 1


def test_browsing_now_playing_stream_download_and_save_create_no_plays(
    history_service,
):
    case = history_service
    service = case.service
    song, album = case.songs[0], case.albums[0]
    for method, values in (
        ("getArtist", {"id": case.artist["id"]}),
        ("getAlbum", {"id": album["id"]}),
        ("getArtistInfo2", {"id": song["id"]}),
        ("getMusicDirectory", {"id": case.artist["id"]}),
        ("getMusicDirectory", {"id": album["id"]}),
        ("search3", {"query": "Daft Punk"}),
    ):
        assert body(get(service, method, **values))["status"] == "ok"
    submit(case, 0, 10, submission="false")
    for method in ("stream", "download"):
        assert get(service, method, id=song["id"]).status_code == 200
    assert body(get(service, "star", id=song["id"]))["status"] == "ok"
    complete_jobs(service)
    assert (
        service[1].state.services.library.downloads(user_id(service))[0][
            "state"
        ]
        == "complete"
    )
    for kind in ("recent", "frequent"):
        assert album_list(case, kind) == []
    doc = body(get(service, "getAlbum", id=album["id"]))["album"]
    assert "played" not in doc and "playCount" not in doc
    assert doc["created"] == album["created"]
    assert "played" not in album_list(case, "newest")[0]
    with service[1].state.services.database.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM scrobbles").fetchone()[0]
            == 0
        )


def test_history_survives_restart_and_account_target_retry(history_service):
    case = history_service
    service = case.service
    submit(case, 0, 10)
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='new-target' WHERE username='admin'"
        )
    submit(case, 0, 10)
    submit(case, 0, 20)
    with service[1].state.services.database.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM scrobbles").fetchone()[0]
            == 3
        )
    assert album_list(case)[0]["playCount"] == 2
    assert (
        body(get(service, "getSong", id=case.songs[0]["id"]))["song"][
            "playCount"
        ]
        == 2
    )
    accounts = make_accounts(service[1].state.config.data_dir)
    library = make_library(accounts, make_catalog(accounts), "aac")
    assert library.played_albums(user_id(service), "recent", 500, 0) == (
        album_list(case)
    )


def test_same_title_album_editions_have_separate_histories(history_service):
    case = history_service
    catalog = case.service[1].state.services.catalog
    stored = catalog.read(case.albums[1]["id"])
    catalog.put(
        "album",
        stored["source"],
        stored["document"]
        | {
            "name": case.albums[0]["name"],
        },
        complete=True,
    )
    submit(case, 0, 10)
    submit(case, 2, 20)
    recent = album_list(case)
    assert [a["id"] for a in recent] == [
        case.albums[1]["id"],
        case.albums[0]["id"],
    ]
    assert recent[0]["name"] == recent[1]["name"]
    assert [a["playCount"] for a in recent] == [1, 1]


@pytest.mark.parametrize("method", ["getAlbumList", "getAlbumList2"])
def test_client_report_and_history_flow_post_xml_head(history_service, method):
    case = history_service
    service = case.service
    params = [
        ("apiKey", service[3]["admin"]),
        ("v", "1.16.1"),
        ("c", "Feishin"),
        ("f", "json"),
        ("id", case.songs[0]["id"]),
        ("id", case.songs[2]["id"]),
        ("time", str(case.stamp * 1000)),
        ("time", str((case.stamp + 60) * 1000)),
    ]
    response = service[0].post(
        "/rest/scrobble.view",
        content=urlencode(params),
        headers={
            "content-type": "application/x-www-form-urlencoded",
        },
    )
    assert body(response)["status"] == "ok"
    field = "albumList2" if method.endswith("2") else "albumList"
    expected = album_list(case, method=method)
    xml = fromstring(get(service, method, type="recent", f="xml").content)
    items = xml.findall("{*}" + field + "/{*}album")
    assert [a.attrib["id"] for a in items] == [a["id"] for a in expected]
    assert [int(a.attrib["playCount"]) for a in items] == [1, 1]
    assert [a.attrib["played"] for a in items] == [
        a["played"] for a in expected
    ]
    assert all(
        datetime.fromisoformat(a["played"]).tzinfo == UTC for a in expected
    )
    head = service[0].head(
        "/rest/" + method,
        params={
            "apiKey": service[3]["admin"],
            "v": "1.16.1",
            "c": "Navic",
            "type": "recent",
        },
    )
    assert head.status_code == 200 and head.content == b""
    assert album_list(case, method=method) == expected


@pytest.mark.parametrize(
    "values",
    [
        {"size": "501"},
        {"size": "bad"},
        {"offset": "-1"},
        {"offset": "bad"},
    ],
)
def test_invalid_history_pagination_is_rejected(history_service, values):
    assert (
        body(
            get(
                history_service.service,
                "getAlbumList2",
                type="recent",
                **values,
            )
        )["error"]["code"]
        == 10
    )


def test_rejected_batch_report_adds_no_album_history(history_service):
    case = history_service
    response = body(
        get(
            case.service,
            "scrobble",
            id=[
                case.songs[0]["id"],
                "missing",
            ],
            time=[case.stamp * 1000, case.stamp * 1000],
        )
    )
    assert response["status"] == "failed"
    assert album_list(case) == []


def test_cached_public_play_fields_cannot_fabricate_history(history_service):
    case = history_service
    catalog = case.service[1].state.services.catalog
    album = catalog.read(case.albums[0]["id"])
    catalog.put(
        "album",
        album["source"],
        album["document"]
        | {
            "playCount": 999,
            "played": "2099-01-01T00:00:00Z",
        },
        complete=True,
    )
    get(case.service, "star", albumId=album["id"])
    assert album_list(case) == []
    assert "played" not in album_list(case, "starred")[0]
    submit(case, 0, 10)
    heard = album_list(case)[0]
    assert heard["playCount"] == 1
    assert (
        datetime.fromisoformat(heard["played"]).timestamp() == case.stamp + 10
    )


@pytest.mark.parametrize("parent", [None, "missing", "artist"])
def test_history_does_not_emit_invalid_album_parents(history_service, parent):
    case = history_service
    submit(case, 0, 10)
    song_id = case.songs[0]["id"]
    with case.service[1].state.services.database.transaction() as connection:
        if parent is None:
            connection.execute(
                "UPDATE catalog SET document=json_remove(document, "
                "'$.albumId') WHERE id=?",
                (song_id,),
            )
        else:
            connection.execute(
                "UPDATE catalog SET document=json_set(document, "
                "'$.albumId', ?) WHERE id=?",
                (case.artist["id"] if parent == "artist" else parent, song_id),
            )
    assert (
        body(get(case.service, "getSong", id=song_id))["song"]["playCount"]
        == 1
    )
    assert album_list(case) == []
