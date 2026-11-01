import copy

import pytest

from support.catalog import SONG, body, get
from support.sync import run, uid


def source(index, title=None):
    return copy.deepcopy(SONG) | {
        "videoId": f"{index:011d}",
        "title": title or f"Track {index}",
        "artists": [{"name": "Test Artist", "id": "UC_test_artist"}],
    }


def pair(index):
    return "Test Artist", f"Track {index}"


@pytest.fixture
def mix_service(sync_service, monkeypatch):
    service = sync_service
    app = service[1]
    for name in ("admin", "alice"):
        app.state.services.mixes.linked(uid(service, name))
    calls = []

    async def taste(client, method, **params):
        calls.append((method, params))
        return {
            "user.getTopTracks": [pair(1)],
            "user.getTopArtists": [("Test Artist", "Test Artist")],
            "user.getRecentTracks": [pair(2), pair(2)],
            "track.getSimilar": [pair(1), pair(3), pair(3), pair(4)],
        }.get(method, [])

    async def fetch(payload):
        query = payload["query"]
        return {
            "songs": [
                source(i) for i in range(1, 9) if query == " ".join(pair(i))
            ]
        }

    monkeypatch.setattr(app.state.services.lastfm, "taste_rows", taste)
    monkeypatch.setattr(app.state.services.catalog.provider, "fetch", fetch)
    return service, calls


def refresh(service):
    assert run(service, service[1].state.services.mixes.refresh_one)


def playlists(service, **params):
    return body(get(service, "getPlaylists", **params))["playlists"][
        "playlist"
    ]


def tracks(service, name, **params):
    item = next(p for p in playlists(service, **params) if p["name"] == name)
    return body(get(service, "getPlaylist", id=item["id"], **params))[
        "playlist"
    ]["entry"]
