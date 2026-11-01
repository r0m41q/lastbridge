import copy

import pytest

from lastbridge.shared.errors import CatalogError
from lastbridge.transport.browser.dependencies import COOKIE
from support.catalog import SONG
from support.library import run

URL = "https://music.youtube.com/playlist?list=PL_test_playlist123"


@pytest.fixture
def imported_service(saved_service):
    client, app, metadata, _ = saved_service
    imports = app.state.services.playlists.imports
    original = metadata.fetch
    state = {
        "title": "Chart <&>",
        "trackCount": 4,
        "tracks": [
            SONG,
            {"title": "Unavailable"},
            SONG | {"videoId": "othertrack1"},
            SONG,
        ],
    }
    calls = []

    async def fetch(payload):
        if payload["operation"] == "playlist":
            calls.append(payload)
            if state.get("fail"):
                raise CatalogError()
            return copy.deepcopy(state)
        return await original(payload)

    imports.provider.fetch = fetch
    yield saved_service, imports, state, calls
    assert not app.state.services.audio.provider.calls


def user(service, name="admin"):
    return service[1].state.services.accounts.user(name)


def browser(service, name="admin"):
    account = service[1].state.services.accounts
    token = account.login(name, "a sufficiently long password", "test")
    service[0].cookies.set(COOKIE, token)
    return account.session(token)["csrf"]


def accept(bundle, *, shared=False, name="admin"):
    service, imports, _, _ = bundle
    owner = user(service, name)
    item_id = run(service, imports.preview, owner, URL, shared)
    return imports.confirm(owner, item_id)


def due(service, item_id):
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE playlist_imports SET next_attempt=0 WHERE id=?",
            (item_id,),
        )
