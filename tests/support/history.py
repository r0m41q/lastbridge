"""Local playback reports and album fixtures shared by history regressions."""

import time
from dataclasses import dataclass

import pytest

from support.audio import OTHER, THIRD
from support.catalog import ARTIST, CREDITS, SONG, body, get, search


@dataclass
class HistoryCase:
    service: tuple
    artist: dict
    albums: list[dict]
    songs: list[dict]
    stamp: int


@pytest.fixture
def history_service(saved_service):
    service = saved_service
    catalog = service[1].state.services.catalog
    result = search(service)
    artist = result["artist"][0]
    first = body(get(service, "getAlbum", id=result["album"][0]["id"]))[
        "album"
    ]
    albums, songs = [first], list(first["song"])
    for source, name, video in (
        ("MPRE_history_b", "Second album", OTHER),
        ("MPRE_history_c", "Unplayed album", THIRD),
    ):
        album = catalog.album(
            {
                "browseId": source,
                "title": name,
                "artists": CREDITS,
                "trackCount": 1,
                "year": "2021",
                "type": "Album",
            }
        )
        song = catalog.song(
            SONG | {"videoId": video, "title": name + " song"}, album, 1
        )
        albums.append(
            catalog.put(
                "album",
                source,
                album
                | {
                    "song": [song],
                    "songCount": 1,
                    "duration": song["duration"],
                },
                complete=True,
            )
        )
        songs.append(song)
    catalog.put(
        "artist",
        ARTIST,
        artist
        | {
            "album": [
                {k: v for k, v in a.items() if k != "song"} for a in albums
            ],
            "albumCount": len(albums),
        },
        complete=True,
        snapshot={
            "version": 1,
            "complete": True,
            "refreshed_at": time.time(),
        },
    )
    # Existing reporting requires a Last.fm target. No provider credentials
    # or running delivery workers are needed for these local acceptance tests.
    with catalog.repository.database.transaction() as connection:
        for name in ("admin", "alice"):
            connection.execute(
                "UPDATE users SET lastfm_name=? WHERE username=?",
                (name + "-fm", name),
            )
    return HistoryCase(service, artist, albums, songs, int(time.time()) - 3600)


def submit(case, song, offset, *, user="admin", **values):
    result = body(
        get(
            case.service,
            "scrobble",
            id=case.songs[song]["id"],
            time=(case.stamp + offset) * 1000,
            apiKey=case.service[3][user],
            **values,
        )
    )
    assert result["status"] == "ok", result
    return result


def album_list(
    case, kind="recent", *, method="getAlbumList2", user="admin", **values
):
    response = body(
        get(
            case.service,
            method,
            type=kind,
            apiKey=case.service[3][user],
            **values,
        )
    )
    assert response["status"] == "ok", response
    return response["albumList2" if method.endswith("2") else "albumList"][
        "album"
    ]
