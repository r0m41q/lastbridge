import copy
from types import SimpleNamespace

from support.catalog import SONG


def browse_run(name, source):
    return {
        "text": name,
        "navigationEndpoint": {"browseEndpoint": {"browseId": source}},
    }


def suggestion(row=None, *, kind="songs"):
    row = row or SONG
    watch = {
        "videoId": row["videoId"],
        "watchEndpointMusicSupportedConfigs": {
            "watchEndpointMusicConfig": {
                "musicVideoType": row.get("videoType")
            }
        },
    }
    columns = [
        [{"text": row["title"]}],
        [browse_run(a["name"], a["id"]) for a in row["artists"]],
        [browse_run(row["album"]["name"], row["album"]["id"])],
    ]
    endpoint = {"watchEndpoint": watch}
    if kind != "songs":
        entity = row["artists"][0] if kind == "artists" else row["album"]
        columns[0] = [{"text": entity["name"]}]
        endpoint = {"browseEndpoint": {"browseId": entity["id"]}}
    return {
        "musicResponsiveListItemRenderer": {
            "navigationEndpoint": endpoint,
            "flexColumns": [
                {
                    "musicResponsiveListItemFlexColumnRenderer": {
                        "text": {"runs": runs}
                    }
                }
                for runs in columns
            ],
            "thumbnail": {
                "musicThumbnailRenderer": {
                    "thumbnail": {"thumbnails": row["thumbnails"]}
                }
            },
            "clickTrackingParams": "private-tracking-value",
        }
    }


def response(*items):
    return {
        "contents": [
            {"searchSuggestionsSectionRenderer": {"contents": list(items)}}
        ]
    }


class SuggestionMusic:
    def __init__(self, recommended=None, rows=None):
        self.recommended = (
            response(suggestion()) if recommended is None else recommended
        )
        self.rows = {"songs": [SONG], "albums": [], "artists": []}
        if rows is not None:
            self.rows.update(rows)
        self.calls = []
        self.details = {}
        self._session = SimpleNamespace(request=lambda *args, **kwargs: None)

    def search(self, query, *, filter, limit):
        self.calls.append(("search", query, filter, limit))
        return copy.deepcopy(self.rows[filter])

    def _send_request(self, endpoint, payload):
        self.calls.append((endpoint, payload))
        if isinstance(self.recommended, Exception):
            raise self.recommended
        return copy.deepcopy(self.recommended)

    def get_song(self, source):
        self.calls.append(("get_song", source))
        details = self.details.get(
            source, {"videoId": source, "lengthSeconds": "337"}
        )
        if isinstance(details, Exception):
            raise details
        return {"videoDetails": details}
