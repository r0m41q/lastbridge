import copy

import httpx
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
    run(service, imports.refresh_one)
    return imports.confirm(owner, item_id)


def due(service, item_id):
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE playlist_imports SET next_attempt=0 WHERE id=?",
            (item_id,),
        )


@pytest.fixture
def lastfm_imported_service(imported_service):
    service, imports, _, _ = imported_service
    with service[1].state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE users SET lastfm_name='lastfm-' || username "
            "WHERE username IN ('admin','alice')"
        )
    state = {
        "rows": [
            {
                "name": SONG["title"],
                "artists": [{"name": SONG["artists"][0]["name"]}],
                "duration": SONG["duration_seconds"],
            }
        ],
        "fail": False,
    }
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if state["fail"]:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "playlist": copy.deepcopy(state["rows"])
                if request.url.params["page"] == "1"
                else []
            },
        )

    provider = imports.lastfm_provider
    original = provider.client
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        yield service, imports, state, calls
    finally:
        run(service, provider.client.aclose)
        provider.client = original


def accept_lastfm(bundle, name="alice"):
    service, imports, _, _ = bundle
    owner = user(service, name)
    item_id = run(service, imports.preview, owner, "12354876")
    run(service, imports.refresh_one)
    return imports.confirm(owner, item_id)


class ChartLookup:
    """Recording resolver with observable concurrency and interruption."""

    def __init__(self, catalog, block_from=None):
        import asyncio

        self.catalog = catalog
        self.block_from = block_from
        self.entered, self.gate = asyncio.Event(), asyncio.Event()
        self.calls, self.active, self.maximum = [], 0, 0
        self.error = None

    async def __call__(self, artist, title):
        import asyncio

        index = int(title.removeprefix("Track "))
        self.calls.append(title)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(0)
            if self.error and index >= 10:
                raise self.error
            if self.block_from is not None and index >= self.block_from:
                self.entered.set()
                await self.gate.wait()
            return self.catalog.song(
                SONG | {"title": title, "videoId": f"{index:011d}"}
            )
        finally:
            self.active -= 1


def chart_tracks(count):
    return [
        SONG
        | {
            "title": f"Track {index}",
            "videoId": f"{index:011d}",
            "videoType": "MUSIC_VIDEO_TYPE_OMV",
        }
        for index in range(count)
    ]
