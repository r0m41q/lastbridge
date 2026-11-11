import copy

import pytest
from fastapi.testclient import TestClient

from lastbridge.config import Settings
from lastbridge.shared.errors import CatalogError
from support.application import make_app as create_app

ARTIST = "UC_test_artist"


ALBUM = "MPRE_test_album"


VIDEO = "khnokW3Mw24"


ART = "https://lh3.googleusercontent.com/cover=w600-h600"


CREDITS = [{"name": "Daft Punk & friends", "id": ARTIST}]


THUMBNAILS = [{"url": ART}]


SONG = {
    "videoId": VIDEO,
    "title": "Instant Crush <studio>",
    "artists": CREDITS,
    "album": {"id": ALBUM, "name": "RAM"},
    "duration_seconds": 337,
    "videoType": "MUSIC_VIDEO_TYPE_ATV",
    "thumbnails": THUMBNAILS,
}


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def fetch(self, payload):
        self.calls.append(payload)
        if self.fail:
            raise CatalogError()
        if payload["operation"] == "search":
            return copy.deepcopy(
                {
                    "songs": [
                        SONG,
                        SONG,
                        SONG
                        | {
                            "videoId": "abcdefghijk",
                            "videoType": "MUSIC_VIDEO_TYPE_OMV",
                        },
                    ],
                    "albums": [
                        {
                            "browseId": ALBUM,
                            "title": "RAM",
                            "artists": CREDITS,
                            "thumbnails": THUMBNAILS,
                        }
                    ],
                    "artists": [
                        {
                            "browseId": ARTIST,
                            "artist": CREDITS[0]["name"],
                            "thumbnails": THUMBNAILS,
                        }
                    ],
                }
            )
        if payload["operation"] == "album":
            return copy.deepcopy(
                {
                    "title": "RAM",
                    "artists": CREDITS,
                    "year": "2013",
                    "thumbnails": THUMBNAILS,
                    "tracks": [
                        SONG | {"album": "RAM"},
                        SONG | {"videoId": "othertrack1", "title": "Other"},
                        SONG
                        | {"videoId": "unavailable", "isAvailable": False},
                    ],
                }
            )
        return copy.deepcopy(
            {
                "name": CREDITS[0]["name"],
                "thumbnails": THUMBNAILS,
                # Subscription channel identity differs from the browse ID.
                "channelId": "UC_DIFFERENT_SUBSCRIPTION_ID",
                "discography_complete": True,
                "albums": {"results": [{"browseId": ALBUM, "title": "RAM"}]},
                "singles": {"results": []},
            }
        )


@pytest.fixture
def catalog_service(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path / "data"))
    with TestClient(app) as client:
        client.portal.call(app.state.services.radio.close)
        store = app.state.services.accounts
        keys = {}
        for index, name in enumerate(("admin", "alice", "unlinked")):
            store.create_user(
                name, "a sufficiently long password", bootstrap=index == 0
            )
            user = store.user(name)
            keys[name] = store.rotate_client_secret(user["id"])
            if name != "unlinked":
                with store.repository.database.transaction() as connection:
                    connection.execute(
                        "UPDATE users SET link_status='linked' WHERE id=?",
                        (user["id"],),
                    )
        provider = app.state.services.catalog.provider
        yield client, app, provider, keys


def get(service, method, **params):
    client, _, _, keys = service
    return client.get(
        "/rest/" + method,
        params={
            "apiKey": keys["admin"],
            "v": "1.16.1",
            "c": "test",
            "f": "json",
            **params,
        },
    )


def body(response):
    return response.json()["subsonic-response"]


def search(service):
    return body(get(service, "search3", query="Daft Punk"))["searchResult3"]
