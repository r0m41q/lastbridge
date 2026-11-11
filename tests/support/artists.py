import copy

import pytest

from lastbridge.shared.errors import CatalogError
from support.catalog import ALBUM, ARTIST, SONG


class ArtistProvider:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.page = {
            "name": "Artist",
            "discography_complete": True,
            "albums": {
                "results": [
                    {"browseId": ALBUM, "title": "Album", "year": "2020"},
                    {"browseId": "MPRE_edition", "title": "Album"},
                ]
            },
            "singles": {
                "results": [
                    {"browseId": "MPRE_single", "title": "Single"},
                    {
                        "browseId": "MPRE_ep",
                        "title": "EP",
                        "type": "EP",
                        "artists": [
                            {"id": "UC_other", "name": "Other"},
                            {"id": ARTIST, "name": "Artist"},
                        ],
                    },
                ]
            },
        }

    async def fetch(self, payload):
        self.calls.append(payload)
        if self.fail:
            raise CatalogError()
        return copy.deepcopy(self.page)


def expire_artist(catalog, item_id):
    with catalog.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE catalog SET document=json_set(document, "
            "'$._artist_snapshot.refreshed_at', 0) WHERE id=?",
            (item_id,),
        )


class RankedMetadata:
    configured = True

    def __init__(self):
        self.calls = []
        self.fail = False
        self.tracks = [
            ("Daft Punk & friends", SONG["title"]),
            ("Daft Punk & friends", "Second"),
        ]
        self.biography = {"biography": "An artist.", "similar": []}

    async def top_tracks(self, name):
        self.calls.append(("tops", name))
        if self.fail:
            raise CatalogError()
        return self.tracks

    async def info(self, name):
        self.calls.append(("info", name))
        if self.fail:
            raise CatalogError()
        return self.biography


@pytest.fixture
def tops_service(catalog_service, monkeypatch):
    service = catalog_service
    catalog = service[1].state.services.catalog
    original = catalog.provider.fetch
    calls = []
    catalog.extras.metadata = RankedMetadata()

    async def fetch(payload):
        if payload.get("kinds") != ["songs"]:
            return await original(payload)
        calls.append(payload)
        return copy.deepcopy(
            {
                "songs": [
                    SONG,
                    SONG,
                    SONG
                    | {
                        "videoId": "othertrack1",
                        "title": "Second",
                        "duration_seconds": None,
                        "duration": "3:04",
                    },
                    SONG | {"videoId": "othertrack2", "isAvailable": False},
                    SONG
                    | {
                        "videoId": "othertrack3",
                        "videoType": "MUSIC_VIDEO_TYPE_OMV",
                    },
                    SONG | {"artists": [{"id": "UC_other", "name": "Other"}]},
                    SONG | {"album": "broken"},
                    SONG | {"videoId": {}},
                ]
            }
        )

    monkeypatch.setattr(catalog.provider, "fetch", fetch)
    return service, calls
