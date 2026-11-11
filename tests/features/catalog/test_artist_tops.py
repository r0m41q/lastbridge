import time
from xml.etree.ElementTree import fromstring

import pytest
from support.catalog import ARTIST, SONG, body, get, search


def test_remote_artist_top_songs_id_name_xml_and_no_downloads(tops_service):
    service, calls = tops_service
    artist = search(service)["artist"][0]
    result = body(get(service, "getTopSongs", id=artist["id"]))["topSongs"][
        "song"
    ]
    assert [s["title"] for s in result] == [SONG["title"], "Second"]
    assert result[1]["duration"] == 184
    assert all(s["contentType"] for s in result)
    assert len(calls) == 2
    assert (
        body(get(service, "getTopSongs", artist=artist["name"], count=1))[
            "topSongs"
        ]["song"]
        == result[:1]
    )
    # ID takes precedence over a conflicting artist name.
    root = fromstring(
        get(
            service,
            "getTopSongs",
            id=artist["id"],
            artist="Wrong artist",
            f="xml",
        ).content
    )
    assert len(root.findall("{*}topSongs/{*}song")) == 2
    assert len(calls) == 2
    store = service[1].state.services.accounts
    with store.repository.database.connection() as connection:
        assert not connection.execute("SELECT * FROM favorites").fetchall()
        assert not connection.execute("SELECT * FROM save_jobs").fetchall()


def test_artist_detail_preserves_rankings_and_cached_stars_stay_private(
    tops_service,
):
    service, calls = tops_service
    artist = search(service)["artist"][0]
    get(service, "getArtist", id=artist["id"])
    songs = body(get(service, "getTopSongs", id=artist["id"]))["topSongs"][
        "song"
    ]
    assert len(calls) == 2
    get(service, "star", id=songs[0]["id"])
    own = body(get(service, "getTopSongs", id=artist["id"]))["topSongs"][
        "song"
    ]
    other = body(
        get(
            service, "getTopSongs", id=artist["id"], apiKey=service[3]["alice"]
        )
    )["topSongs"]["song"]
    assert own[0]["starred"]
    assert "starred" not in other[0]
    assert len(calls) == 2


def test_top_songs_name_can_resolve_an_unsaved_artist(tops_service):
    service, calls = tops_service
    songs = body(get(service, "getTopSongs", artist="Daft Punk & friends"))[
        "topSongs"
    ]["song"]
    assert len(songs) == 2 and len(calls) == 2


@pytest.mark.parametrize(
    "params,code",
    [
        ({}, 10),
        ({"id": ""}, 10),
        ({"artist": " "}, 10),
        ({"id": "unknown"}, 70),
        ({"artist": "Name", "count": -1}, 10),
    ],
)
def test_top_songs_bad_parameters(tops_service, params, code):
    assert (
        body(get(tops_service[0], "getTopSongs", **params))["error"]["code"]
        == code
    )


def test_zero_count_head_and_outage_are_bounded(tops_service):
    service, calls = tops_service
    client, app, _, keys = service
    artist = search(service)["artist"][0]
    assert not body(get(service, "getTopSongs", id=artist["id"], count=0))[
        "topSongs"
    ]["song"]
    assert (
        client.head(
            "/rest/getTopSongs",
            params={
                "apiKey": keys["admin"],
                "v": "1",
                "c": "test",
                "id": artist["id"],
            },
        ).status_code
        == 200
    )
    assert calls == []
    original = body(get(service, "getTopSongs", id=artist["id"]))["topSongs"]
    extras = app.state.services.catalog.extras
    cached = extras.tops[ARTIST]
    extras.tops[ARTIST] = (
        time.monotonic() - 1,
        cached[1],
    )

    extras.metadata.fail = True
    assert body(get(service, "getTopSongs", id=artist["id"]))["topSongs"] == (
        original
    )
    extras.tops[ARTIST] = (
        time.monotonic() - 90000,
        cached[1],
    )
    assert (
        body(get(service, "getTopSongs", id=artist["id"]))["error"]["code"]
        == 0
    )
