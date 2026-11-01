import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from lastbridge.config import Settings
from lastbridge.providers.lastfm.client import Lastfm
from support.application import make_app as create_app
from support.build_accounts import make_accounts

PASSWORD = "browser password for tests"


TOKEN = "a" * 32


LASTFM_SESSION = "c" * 32


PARAMS = {"v": "1.16.1", "c": "test", "f": "json"}


def csrf(response) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


def login(client, username="admin", password=PASSWORD):
    response = client.get("/login")
    return client.post(
        "/login",
        data={
            "csrf": csrf(response),
            "username": username,
            "password": password,
        },
        follow_redirects=False,
    )


def start_link(client):
    response = client.post(
        "/lastfm/link",
        data={"csrf": csrf(client.get("/"))},
        follow_redirects=False,
    )
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    callback = query["cb"][0]
    return parse_qs(urlsplit(callback).query)["state"][0]


def finish_link(client, state, token=TOKEN):
    return client.get(
        "/lastfm/callback",
        params={"state": state, "token": token},
        follow_redirects=False,
    )


@pytest.fixture
def service(tmp_path, monkeypatch):
    store = make_accounts(tmp_path / "data")
    store.create_user("admin", PASSWORD, bootstrap=True)
    store.create_user("alice", PASSWORD)
    calls = []

    async def exchange(self, client, token):
        calls.append(token)
        return "Lastfm-" + token[0], LASTFM_SESSION

    monkeypatch.setattr(Lastfm, "exchange", exchange)

    async def loved(self, client, name, page):
        return [], 0

    monkeypatch.setattr(Lastfm, "loved", loved)

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
        yield client, app, calls


def drop_playlist_tables(connection):
    connection.execute("DROP TABLE playlist_imports")
    connection.execute("DROP TABLE mix_activity")
    connection.execute("DROP VIEW retained_tracks")
    connection.execute("DROP TABLE playlist_entries")
    connection.execute("DROP TABLE playlists")


def drop_sync_tables(connection):
    drop_playlist_tables(connection)
    connection.execute("DROP TABLE lyrics")
    connection.execute("DROP TABLE radio")
    connection.execute("DROP TABLE mixes")
    for table in (
        "playback_starts",
        "scrobbles",
        "love_imports",
        "loved_items",
        "favorite_removals",
        "love_removals",
    ):
        connection.execute(f"DROP TABLE {table}")
