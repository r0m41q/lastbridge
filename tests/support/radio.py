import copy

import pytest

from lastbridge.radio import Radio
from support.catalog import SONG, body, get, search


def track(index, **changes):
    return (
        copy.deepcopy(SONG)
        | {
            "videoId": f"{index:011d}",
            "title": f"Track {index}",
            "length": "3:04",
            "duration_seconds": None,
        }
        | changes
    )


@pytest.fixture
def radio_service(sync_service, monkeypatch):
    service = sync_service
    app = service[1]
    app.state.radio = Radio(
        app.state.accounts, app.state.catalog, app.state.library
    )
    calls = []
    original = app.state.catalog.provider.fetch

    async def fetch(payload):
        if payload["operation"] != "radio":
            return await original(payload)
        calls.append(payload)
        return {
            "tracks": [
                SONG,
                track(1),
                track(1),
                track(2),
                track(3),
                track(4, isAvailable=False),
                track(5, videoType="MUSIC_VIDEO_TYPE_OMV"),
            ]
        }

    monkeypatch.setattr(app.state.catalog.provider, "fetch", fetch)
    search(service)
    return service, calls


def request(service, **params):
    seed = search(service)["song"][0]
    return get(service, "getSimilarSongs", id=seed["id"], **params)


def selected(response, field="similarSongs"):
    return body(response)[field]["song"]
