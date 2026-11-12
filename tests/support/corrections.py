import copy

import pytest

from support.catalog import SONG
from support.imports import browser
from support.library import user_id

ALTERNATE = "newaudio123"


@pytest.fixture
def correction_service(saved_service):
    service = saved_service
    catalog = service[1].state.services.catalog
    services = service[1].state.services
    with services.database.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='lastfm-admin',lastfm_secret=? "
            "WHERE username='admin'",
            (services.credentials.encrypt("test-session"),),
        )
    original = catalog.song(SONG)
    state = {
        "rows": [
            SONG
            | {
                "videoId": ALTERNATE,
                "album": {"id": "MPRE_alternate", "name": "Another release"},
                "isExplicit": True,
            }
        ],
        "calls": [],
    }

    async def fetch(payload):
        state["calls"].append(payload)
        if state.get("failure"):
            raise state["failure"]
        return {"songs": copy.deepcopy(state["rows"])}

    service[1].state.services.corrections.provider.fetch = fetch
    return service, original, state


def search_recordings(bundle, kind="song", key=None, name="admin"):
    service, original, _ = bundle
    key = original["id"] if key is None else key
    token = browser(service, name)
    path = f"/library/recordings/{kind}/{key}"
    response = service[0].post(path + "/search", data={"csrf": token})
    assert response.status_code == 200, response.text
    review = service[1].state.services.corrections.report(
        user_id(service, name), kind, key
    )["review"]
    return token, path, review


def choose_recording(
    bundle, *, kind="song", key=None, name="admin", acknowledge=False
):
    service, original, _ = bundle
    token, path, review = search_recordings(bundle, kind, key, name)
    assert review["candidates"]
    response = service[0].post(
        path + "/choose",
        data={
            "csrf": token,
            "token": review["token"],
            "song_id": review["candidates"][0]["song_id"],
            "acknowledge": "yes" if acknowledge else "no",
        },
    )
    assert response.status_code == 200, response.text
    return review["candidates"][0]


def import_love(
    service, artist="Last.fm artist", title="Missing song", song_id=None
):
    from lastbridge.shared.recordings import recording_key

    sync = service[1].state.services.sync
    account = service[1].state.services.accounts.user("admin")
    sync.linked(account["id"])
    key = recording_key(artist, title)
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "INSERT INTO loved_items(user_id,target,recording_key,artist,"
            "title,state,song_id) VALUES(?,?,?,?,?,?,?)",
            (
                account["id"],
                account["lastfm_name"].casefold(),
                key,
                artist,
                title,
                "matched" if song_id else "unmatched",
                song_id,
            ),
        )
    return key
