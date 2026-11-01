import time

import httpx
import pytest
from fastapi.testclient import TestClient

from lastbridge.config import Settings
from lastbridge.lastfm import Lastfm
from support.accounts import PASSWORD
from support.application import make_app as create_app
from support.catalog import get, search


def accepted(code=0):
    return {
        "scrobbles": {
            "@attr": {
                "accepted": str(int(code == 0)),
                "ignored": str(int(code != 0)),
            },
            "scrobble": {
                "ignoredMessage": {"code": str(code), "#text": "secret"}
            },
        }
    }


def page(tracks, current=1, total=1):
    return {
        "lovedtracks": {
            "@attr": {"page": str(current), "totalPages": str(total)},
            "track": [
                {"artist": {"name": artist}, "name": title}
                for artist, title in tracks
            ],
        }
    }


@pytest.fixture
def sync_service(tmp_path, monkeypatch):
    async def taste_rows(self, client, method, **parameters):
        return []

    monkeypatch.setattr(Lastfm, "taste_rows", taste_rows)
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            public_url="http://testserver",
            lastfm_api_key="1" * 32,
            lastfm_api_secret="2" * 32,
        )
    )
    with TestClient(app) as client:
        client.portal.call(app.state.radio.close)
        keys = {}
        for index, name in enumerate(("admin", "alice")):
            app.state.accounts.create_user(
                name, PASSWORD, bootstrap=index == 0
            )
            user = app.state.accounts.user(name)
            with app.state.accounts.transaction() as connection:
                connection.execute(
                    "UPDATE users SET lastfm_name=?,lastfm_secret=?,"
                    "link_status='linked' WHERE id=?",
                    (
                        name + "-fm",
                        app.state.accounts.encrypt(name + "-session"),
                        user["id"],
                    ),
                )
            keys[name] = app.state.accounts.rotate_client_secret(user["id"])
            app.state.sync.linked(user["id"])
        yield client, app, app.state.catalog.provider, keys


def run(service, function, *args):
    return service[0].portal.call(function, *args)


def uid(service, name="admin"):
    return service[1].state.accounts.user(name)["id"]


def rows(service, table="scrobbles"):
    with service[1].state.accounts.connection() as connection:
        return [
            dict(row) for row in connection.execute(f"SELECT * FROM {table}")
        ]


def due(service):
    with service[1].state.accounts.transaction() as connection:
        connection.execute("UPDATE love_imports SET next_attempt=0")
        connection.execute("UPDATE loved_items SET next_attempt=0")
        connection.execute("UPDATE scrobbles SET next_attempt=0")


def transport(service, handler):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            service[1].state.sync.remote.client = client
            await service[1].state.sync.deliver_one()

    return run(service, scenario)


def report(service, song=None, **params):
    song = song or search(service)["song"][0]
    return get(
        service,
        "scrobble",
        id=song["id"],
        time=int((time.time() - 180) * 1000),
        **params,
    )


def import_steps(service, handler, count=1):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            service[1].state.sync.remote.client = client
            for _ in range(count):
                due(service)
                await service[1].state.sync.import_one()

    run(service, scenario)
