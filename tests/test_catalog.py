import asyncio
import copy
import json
import sys
from xml.etree.ElementTree import fromstring

import httpx
import pytest
from fastapi.testclient import TestClient

from lastbridge.accounts import Accounts
from lastbridge.app import create_app
from lastbridge.catalog import (
    Catalog,
    CatalogError,
    MetadataProvider,
    identity,
    image_url,
)
from lastbridge.config import Settings

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
                "albums": {"results": [{"browseId": ALBUM, "title": "RAM"}]},
                "singles": {"results": []},
            }
        )


@pytest.fixture
def catalog_service(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path / "data"))
    with TestClient(app) as client:
        store = app.state.accounts
        keys = {}
        for index, name in enumerate(("admin", "alice", "unlinked")):
            store.create_user(
                name, "a sufficiently long password", bootstrap=index == 0
            )
            user = store.user(name)
            keys[name] = store.rotate_client_secret(user["id"])
            if name != "unlinked":
                with store.transaction() as connection:
                    connection.execute(
                        "UPDATE users SET link_status='linked' WHERE id=?",
                        (user["id"],),
                    )
        provider = FakeProvider()
        app.state.catalog.provider = provider
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


def test_public_extensions_only_advertise_implemented_features(
    catalog_service,
):
    client = catalog_service[0]
    data = body(client.get("/rest/getOpenSubsonicExtensions.view?f=json"))
    assert data["openSubsonicExtensions"] == [
        {"name": "apiKeyAuthentication", "versions": [1]},
        {"name": "formPost", "versions": [1]},
    ]
    root = fromstring(client.get("/rest/getOpenSubsonicExtensions").content)
    ns = {"s": "http://subsonic.org/restapi"}
    assert root.find("s:openSubsonicExtensions/s:versions", ns).text == "1"


def test_relations_artists_albums_songs_and_restart(catalog_service):
    _, app, provider, _ = catalog_service
    result = search(catalog_service)
    assert len(result["song"]) == 1  # deduplicated; video rejected
    song, album, artist = (result[k][0] for k in ("song", "album", "artist"))
    assert song["albumId"] == album["id"]
    assert song["parent"] == album["id"]
    assert song["artistId"] == album["artistId"] == artist["id"]
    assert song["id"] != album["id"] != artist["id"]
    detail = body(get(catalog_service, "getAlbum", id=album["id"]))["album"]
    assert detail["songCount"] == 2
    assert detail["duration"] == 674
    assert [s["track"] for s in detail["song"]] == [1, 2]
    assert detail["song"][0]["id"] == song["id"]
    assert (
        body(get(catalog_service, "getSong", id=song["id"]))["song"]["year"]
        == 2013
    )
    artist_detail = body(get(catalog_service, "getArtist", id=artist["id"]))[
        "artist"
    ]
    assert artist_detail["album"][0]["id"] == album["id"]
    assert artist_detail["id"] == artist["id"]
    store = Accounts(app.state.config.data_dir)
    restored = Catalog(store, provider)
    assert asyncio.run(restored.detail(album["id"], "album")) == detail
    assert restored.read(song["id"])["source"] == VIDEO
    # Sparse subsequent search data must preserve hydrated track/year/ordering.
    restored.song(SONG)
    assert restored.read(song["id"])["document"]["track"] == 1
    assert "http" not in json.dumps(result)


@pytest.mark.parametrize(
    "method,field",
    [("search2", "searchResult2"), ("search3", "searchResult3")],
)
def test_xml_json_equivalence_and_escaped_text(catalog_service, method, field):
    data = body(get(catalog_service, method, query="Daft Punk"))[field]
    response = get(
        catalog_service, method + ".view", query="Daft Punk", f="xml"
    )
    root = fromstring(response.content)
    ns = {"s": "http://subsonic.org/restapi"}
    song = root.find(f"s:{field}/s:song", ns)
    assert song.attrib["title"] == data["song"][0]["title"]
    assert song.attrib["isDir"] == "false"
    assert song.attrib["id"] == data["song"][0]["id"]
    album = root.find(f"s:{field}/s:album", ns)
    assert album.attrib["id"] == data["album"][0]["id"]
    if method == "search2":
        assert album.attrib["title"] == "RAM"
        assert album.attrib["isDir"] == "true"


def test_search_never_populates_saved_library_or_cross_user_state(
    catalog_service,
):
    _, _, provider, keys = catalog_service
    search(catalog_service)
    count = len(provider.calls)
    for key in (keys["admin"], keys["alice"]):
        for query in ("", '""', "   "):
            result = body(
                get(catalog_service, "search3", apiKey=key, query=query)
            )
            assert result["searchResult3"] == {
                "artist": [],
                "album": [],
                "song": [],
            }
        assert (
            body(get(catalog_service, "getArtists", apiKey=key))["artists"][
                "index"
            ]
            == []
        )
        assert (
            body(
                get(
                    catalog_service, "getAlbumList2", apiKey=key, type="newest"
                )
            )["albumList2"]["album"]
            == []
        )
        assert (
            body(get(catalog_service, "getStarred2", apiKey=key))["starred2"][
                "song"
            ]
            == []
        )
    assert len(provider.calls) == count
    # Public provider metadata is retrievable by another linked user. This
    # permission does not expose private saved-library membership or queries.
    song = identity("song", VIDEO)
    assert (
        body(get(catalog_service, "getSong", apiKey=keys["alice"], id=song))[
            "song"
        ]["id"]
        == song
    )


def test_search_paging_snapshot_and_zero_counts(catalog_service):
    _, app, provider, _ = catalog_service
    first = search(catalog_service)
    second = body(
        get(catalog_service, "search3", query="Daft Punk", songOffset=1)
    )
    assert second["searchResult3"]["song"] == []
    assert len(provider.calls) == 1
    assert first["song"]
    result = body(
        get(
            catalog_service,
            "search3",
            query="another",
            artistCount=0,
            albumCount=0,
            songCount=0,
        )
    )
    assert result["searchResult3"]["song"] == []
    assert len(provider.calls) == 1
    for i in range(35):
        asyncio.run(app.state.catalog.search(i, "query", ["songs"]))
    assert len(app.state.catalog.searches) == 32


@pytest.mark.parametrize(
    "method,params,code",
    [
        ("search3", {}, 10),
        ("search3", {"query": "x", "songCount": -1}, 10),
        ("search3", {"query": "x", "albumOffset": "bad"}, 10),
        ("search3", {"query": "x", "songCount": 101}, 10),
        ("search3", {"query": "x" * 257}, 10),
        ("search3", {"query": "x", "musicFolderId": 2}, 70),
        ("getSong", {}, 10),
        ("getSong", {"id": "missing"}, 70),
        ("getAlbumList2", {"type": "not-real"}, 10),
        ("getCoverArt", {"id": "missing"}, 70),
        ("getCoverArt", {"id": "x", "size": 9000}, 10),
        ("getPlaylist", {"id": "missing"}, 70),
        ("stream", {"id": "missing"}, 0),
    ],
)
def test_errors(catalog_service, method, params, code):
    assert (
        body(get(catalog_service, method, **params))["error"]["code"] == code
    )


def test_wrong_type_missing_and_provider_outage(catalog_service):
    _, _, provider, _ = catalog_service
    result = search(catalog_service)
    song = result["song"][0]
    assert (
        body(get(catalog_service, "getArtist", id=song["id"]))["error"]["code"]
        == 70
    )
    provider.fail = True
    assert body(get(catalog_service, "getSong", id=song["id"]))["song"] == song
    error = body(get(catalog_service, "getAlbum", id=song["albumId"]))["error"]
    assert error == {"code": 0, "message": "Metadata provider unavailable"}
    assert (
        body(get(catalog_service, "search3", query="uncached"))["status"]
        == "failed"
    )
    assert catalog_service[0].get("/health/ready").status_code == 200


def test_authentication_required_for_all_catalog_and_artwork(catalog_service):
    client, _, provider, keys = catalog_service
    for method in (
        "search3",
        "getCoverArt",
        "getAlbum",
        "getArtist",
        "getSong",
        "getMusicFolders",
    ):
        assert (
            body(client.get("/rest/" + method + "?f=json"))["error"]["code"]
            == 40
        )
        assert (
            body(get(catalog_service, method, apiKey=keys["unlinked"]))[
                "error"
            ]["code"]
            == 50
        )
    assert provider.calls == []


def test_profile_roles_and_browsing_post(catalog_service):
    client, _, _, keys = catalog_service
    user = body(get(catalog_service, "getUser", username="admin"))["user"]
    assert user["folder"] == [1]
    assert user["streamRole"] is False
    assert user["scrobblingEnabled"] is False
    assert "lastfm_name" not in user
    assert (
        body(get(catalog_service, "getUser", username="alice"))["error"][
            "code"
        ]
        == 50
    )
    assert (
        body(get(catalog_service, "getPlaylists", username="alice"))["error"][
            "code"
        ]
        == 50
    )
    response = client.post(
        "/rest/getMusicFolders.view",
        data={
            "apiKey": keys["admin"],
            "v": "1.16.1",
            "c": "test",
            "f": "json",
        },
    )
    assert body(response)["musicFolders"]["musicFolder"][0]["id"] == 1
    assert (
        body(get(catalog_service, "getMusicDirectory", id="1"))["directory"][
            "child"
        ]
        == []
    )
    result = search(catalog_service)
    artist_dir = body(
        get(catalog_service, "getMusicDirectory", id=result["artist"][0]["id"])
    )["directory"]
    assert artist_dir["child"][0]["isDir"] is True
    album_dir = body(
        get(catalog_service, "getMusicDirectory", id=result["album"][0]["id"])
    )["directory"]
    assert album_dir["child"][0]["isDir"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://lh3.googleusercontent.com/a",
        "https://localhost/a",
        "https://lh3.googleusercontent.com.attacker.test/a",
        "https://lh3.googleusercontent.com@127.0.0.1/a",
        "https://lh3.googleusercontent.com:8443/a",
        "file:///etc/passwd",
        "https://lh3.googleusercontent.com:bad/a",
    ],
)
def test_artwork_host_restrictions(url):
    assert image_url(url) is None


def test_artwork_proxy_bounded_and_no_redirects(catalog_service):
    _, app, _, _ = catalog_service
    song = search(catalog_service)["song"][0]
    seen = []

    async def run(status=200, headers=None, data=b"image fixture"):
        def handle(request):
            seen.append(request)
            assert str(request.url) == ART
            assert "apiKey" not in str(request.url)
            return httpx.Response(
                status,
                headers=headers or {"content-type": "image/jpeg"},
                content=data,
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle)
        ) as client:
            return await app.state.catalog.artwork(client, song["id"])

    assert asyncio.run(run()) == (b"image fixture", "image/jpeg")
    with pytest.raises(CatalogError, match="provider unavailable"):
        asyncio.run(run(302, {"location": "http://127.0.0.1/private"}))
    assert len(seen) == 2
    with pytest.raises(CatalogError, match="Artwork unavailable"):
        asyncio.run(run(headers={"content-type": "text/html"}))
    with pytest.raises(CatalogError, match="size limit"):
        asyncio.run(run(data=b"x" * (2 * 1024 * 1024 + 1)))


def test_migration_from_v1_preserves_accounts(tmp_path):
    store = Accounts(tmp_path)
    store.create_user("admin", "a sufficiently long password", bootstrap=True)
    with store.transaction() as connection:
        connection.execute("DROP TABLE catalog")
        connection.execute("PRAGMA user_version=1")
    migrated = Accounts(tmp_path)
    assert migrated.user("admin")["admin"] == 1
    with migrated.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            connection.execute("SELECT count(*) FROM catalog").fetchone()[0]
            == 0
        )


def test_provider_timeout_kills_worker_and_releases_slot(monkeypatch):
    original = asyncio.create_subprocess_exec
    processes = []

    async def slow(*args, **kwargs):
        process = await original(
            sys.executable, "-c", "import time; time.sleep(30)", **kwargs
        )
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow)

    async def run():
        provider = MetadataProvider(timeout=0.05)
        with pytest.raises(CatalogError):
            await provider.fetch({"operation": "search"})
        assert processes[0].returncode is not None
        assert provider.slots._value == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    "script,match",
    [
        ("import sys; sys.stdin.read(); print('x'*9000000)", "size limit"),
        ("import sys; sys.stdin.read(); print('[]')", "unavailable"),
        ("import sys; sys.stdin.read(); print('not json')", "unavailable"),
    ],
)
def test_worker_response_limits_and_cleanup(monkeypatch, script, match):
    original = asyncio.create_subprocess_exec
    processes = []

    async def worker(*args, **kwargs):
        process = await original(sys.executable, "-c", script, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", worker)

    async def run():
        provider = MetadataProvider(timeout=3)
        with pytest.raises(CatalogError, match=match):
            await provider.fetch({"operation": "search"})
        assert processes[0].returncode is not None
        assert provider.slots._value == 2

    asyncio.run(run())


def test_worker_concurrency_rejects_excess_waiters(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("No subprocess should be started")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected)

    async def run():
        provider = MetadataProvider()
        await provider.slots.acquire()
        await provider.slots.acquire()
        with pytest.raises(CatalogError, match="busy"):
            await provider.fetch({})
        provider.slots.release()
        provider.slots.release()

    asyncio.run(run())


def test_album_required_fields_and_created_date_survive_updates(
    catalog_service,
):
    result = search(catalog_service)
    album = result["album"][0]
    assert (
        set(("id", "name", "songCount", "duration", "created")) <= album.keys()
    )
    detailed = body(get(catalog_service, "getAlbum", id=album["id"]))["album"]
    assert detailed["created"] == album["created"]


def test_artwork_http_response_and_xml_profile(catalog_service):
    client, app, _, _ = catalog_service
    item = search(catalog_service)["song"][0]
    original = app.state.http_client
    mock = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                headers={"content-type": "image/jpeg"},
                content=b"image fixture",
            )
        )
    )
    app.state.http_client = mock
    try:
        response = get(
            catalog_service, "getCoverArt.view", id=item["id"], size=200
        )
        assert response.content == b"image fixture"
        assert response.headers["content-type"] == "image/jpeg"
        assert "location" not in response.headers
    finally:
        app.state.http_client = original
        asyncio.run(mock.aclose())
    root = fromstring(
        get(catalog_service, "getUser", username="admin", f="xml").content
    )
    user = root.find("{http://subsonic.org/restapi}user")
    assert user.attrib["scrobblingEnabled"] == "false"
    assert user.find("{http://subsonic.org/restapi}folder").text == "1"


def test_unresolved_tracks_have_generic_content_type_for_feishin(
    catalog_service,
):
    result = search(catalog_service)
    song = result["song"][0]
    assert song["contentType"] == "application/octet-stream"
    album = body(get(catalog_service, "getAlbum", id=song["albumId"]))["album"]
    assert all(
        s["contentType"] == "application/octet-stream" for s in album["song"]
    )
    assert all("bitRate" not in s and "suffix" not in s for s in album["song"])


def test_cached_album_and_song_content_type_without_metadata_refetch(
    catalog_service,
):
    _, app, provider, _ = catalog_service
    result = search(catalog_service)
    song = result["song"][0]
    album = body(get(catalog_service, "getAlbum", id=song["albumId"]))["album"]
    # Reproduce existing persisted metadata from before the compatibility fix.
    with app.state.accounts.transaction() as connection:
        rows = connection.execute("SELECT id,document FROM catalog").fetchall()
        for row in rows:
            document = json.loads(row["document"])
            document.pop("contentType", None)
            for entry in document.get("song", []):
                entry.pop("contentType", None)
            connection.execute(
                "UPDATE catalog SET document=? WHERE id=?",
                (json.dumps(document), row["id"]),
            )
    provider.fail = True
    fetched = body(get(catalog_service, "getAlbum", id=album["id"]))["album"]
    assert all(
        s["contentType"] == "application/octet-stream" for s in fetched["song"]
    )
    fetched_song = body(get(catalog_service, "getSong", id=song["id"]))["song"]
    assert fetched_song["contentType"] == "application/octet-stream"
    assert fetched_song["id"] == song["id"]
    # Folder-style browsing must supply the same contract, including XML.
    root = fromstring(
        get(
            catalog_service, "getMusicDirectory", id=album["id"], f="xml"
        ).content
    )
    child = root.find(
        "{http://subsonic.org/restapi}directory/{http://subsonic.org/restapi}child"
    )
    assert child.attrib["contentType"] == "application/octet-stream"
