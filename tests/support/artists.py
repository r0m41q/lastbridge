import copy

from lastbridge.shared.errors import CatalogError
from support.catalog import ALBUM, ARTIST


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
