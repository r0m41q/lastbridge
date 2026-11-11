import asyncio
import copy
import json
from types import SimpleNamespace
from xml.etree.ElementTree import fromstring

import pytest
from support.artists import ArtistProvider, expire_artist
from support.build_catalog import make_catalog
from support.catalog import ALBUM, ARTIST, body, get, search

from lastbridge.features.catalog.adapters.youtube.artists import (
    RELEASE_LIMIT,
    artist_releases,
)
from lastbridge.features.catalog.adapters.youtube.worker import execute
from lastbridge.shared.errors import CatalogError


def test_artist_worker_follows_each_shelf_pointer_and_pagination(monkeypatch):
    calls = []
    page = {
        "name": "Artist",
        "channelId": "subscription_id",
        "albums": {
            "browseId": "UC_albums",
            "params": "albums-token",
            "results": [{"browseId": ALBUM}],
        },
        "singles": {
            "browseId": "UC_singles",
            "params": "singles-token",
            "results": [],
        },
    }

    def releases(source, params, *, limit):
        calls.append((source, params, limit))
        return [
            {"browseId": ALBUM if i == 0 else f"MPRE_{source}_{i}"}
            for i in range(150)
        ]

    monkeypatch.setattr(
        "ytmusicapi.YTMusic",
        lambda: SimpleNamespace(
            get_artist=lambda source: copy.deepcopy(page),
            get_artist_albums=releases,
        ),
    )
    expanded = execute(
        {"operation": "artist", "source": ARTIST, "releases": True}
    )
    assert calls == [
        ("UC_albums", "albums-token", RELEASE_LIMIT + 1),
        ("UC_singles", "singles-token", RELEASE_LIMIT + 1),
    ]
    assert expanded["discography_complete"] is True
    assert len(expanded["albums"]["results"]) == 150
    assert "params" not in expanded["albums"]
    # Top-song reads do not trigger discography enumeration.
    assert execute({"operation": "artist", "source": ARTIST}) == page
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["error", "malformed", "cap", "missing"])
def test_partial_shelf_keeps_preview_and_does_not_claim_complete(failure):
    page = {
        "name": "Artist",
        "albums": {
            "browseId": ARTIST,
            "params": "token",
            "results": [{"browseId": ALBUM}],
        },
        "singles": {"results": [{"browseId": "MPRE_single"}]},
    }

    def releases(*args, **kwargs):
        if failure == "error":
            raise OSError("provider failed")
        if failure == "malformed":
            return None
        if failure == "missing":
            return [{"browseId": "MPRE_other"}]
        return [{"browseId": ALBUM}] + [
            {"browseId": f"MPRE_{i}"} for i in range(RELEASE_LIMIT)
        ]

    row = artist_releases(
        SimpleNamespace(
            get_artist=lambda source: page,
            get_artist_albums=releases,
        ),
        ARTIST,
    )
    assert row["discography_complete"] is False
    assert len(row["albums"]["results"]) <= RELEASE_LIMIT
    assert {"browseId": ALBUM} in row["albums"]["results"]
    assert row["singles"]["results"] == [{"browseId": "MPRE_single"}]


def test_artist_preserves_editions_release_types_dates_and_credits(
    catalog_service,
):
    catalog = catalog_service[1].state.services.catalog
    provider = ArtistProvider()
    catalog.provider = provider
    artist = catalog.artist({"id": ARTIST, "name": "Artist"})
    result = body(get(catalog_service, "getArtist", id=artist["id"]))["artist"]
    albums = result["album"]
    assert result["albumCount"] == 4
    assert albums[0]["name"] == albums[1]["name"]
    assert albums[0]["id"] != albums[1]["id"]
    assert [a["releaseTypes"] for a in albums] == [
        ["album"],
        ["album"],
        ["single"],
        ["ep"],
    ]
    assert albums[0]["releaseDate"] == {"year": 2020}
    assert "originalReleaseDate" not in albums[0]
    assert albums[3]["artist"] == "Other, Artist"
    assert albums[3]["artists"][0]["name"] == "Other"
    assert albums[3]["artistId"] == albums[3]["artists"][0]["id"]
    assert albums[0]["artistId"] == artist["id"]
    assert all("song" not in a for a in albums)
    assert provider.calls == [
        {"operation": "artist", "source": ARTIST, "releases": True}
    ]
    xml = fromstring(
        get(catalog_service, "getArtist", id=artist["id"], f="xml").content
    )
    assert xml.find("{*}artist/{*}album/{*}releaseTypes").text == "album"
    assert xml.find("{*}artist/{*}album/{*}releaseDate").attrib == {
        "year": "2020"
    }
    assert "_artist_snapshot" not in json.dumps(result)
    for album in albums:
        assert "played" not in album and "playCount" not in album
    with catalog.repository.database.connection() as connection:
        for table in ("scrobbles", "favorites", "save_jobs", "love_outbox"):
            assert (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[
                    0
                ]
                == 0
            )


def test_artist_does_not_truncate_to_preview_limit(catalog_service):
    catalog = catalog_service[1].state.services.catalog
    provider = ArtistProvider()
    provider.page["albums"]["results"] = [
        {"browseId": f"MPRE_{i}", "title": f"Album {i}"} for i in range(150)
    ]
    catalog.provider = provider
    artist = catalog.artist({"id": ARTIST, "name": "Artist"})
    result = body(get(catalog_service, "getArtist", id=artist["id"]))
    assert result["artist"]["albumCount"] == 152


def test_artist_snapshot_survives_restart_and_sparse_updates(catalog_service):
    _, app, _, _ = catalog_service
    catalog = app.state.services.catalog
    provider = ArtistProvider()
    catalog.provider = provider
    artist = catalog.artist({"id": ARTIST, "name": "Artist"})
    original = asyncio.run(catalog.detail(artist["id"], "artist"))
    catalog.artist({"id": ARTIST, "name": "Artist"})
    restored = make_catalog(app.state.services.accounts, provider)
    assert asyncio.run(restored.detail(artist["id"], "artist")) == original
    assert len(provider.calls) == 1
    expire_artist(restored, artist["id"])
    # A sparse search must not make an expired snapshot fresh again.
    restored.artist({"id": ARTIST, "name": "Artist"})
    provider.page["singles"]["results"].append(
        {"browseId": "MPRE_new", "title": "New Single"}
    )
    refreshed = asyncio.run(restored.detail(artist["id"], "artist"))
    assert refreshed["albumCount"] == 5
    assert len(provider.calls) == 2


def test_old_complete_artist_is_lazily_upgraded(catalog_service):
    catalog = catalog_service[1].state.services.catalog
    catalog.provider = ArtistProvider()
    artist = catalog.put(
        "artist",
        ARTIST,
        {
            "name": "Artist",
            "album": [],
            "albumCount": 0,
        },
        complete=True,
    )
    result = asyncio.run(catalog.detail(artist["id"], "artist"))
    assert result["albumCount"] == 4
    assert catalog.read(artist["id"])["snapshot"]["complete"] is True


def test_provider_outage_retains_snapshot_and_backs_off(catalog_service):
    catalog = catalog_service[1].state.services.catalog
    provider = ArtistProvider()
    catalog.provider = provider
    artist = catalog.artist({"id": ARTIST, "name": "Artist"})
    original = asyncio.run(catalog.detail(artist["id"], "artist"))
    expire_artist(catalog, artist["id"])
    provider.fail = True
    assert asyncio.run(catalog.detail(artist["id"], "artist")) == original
    assert asyncio.run(catalog.detail(artist["id"], "artist")) == original
    assert len(provider.calls) == 2
    assert catalog.read(artist["id"])["snapshot"]["refreshed_at"] == 0
    # An initial outage must not cache an empty successful artist page.
    new = catalog.artist({"id": "UC_new", "name": "New"})
    with pytest.raises(CatalogError):
        asyncio.run(catalog.detail(new["id"], "artist"))


def test_partial_refresh_preserves_releases_and_retries(catalog_service):
    catalog = catalog_service[1].state.services.catalog
    provider = ArtistProvider()
    catalog.provider = provider
    artist = catalog.artist({"id": ARTIST, "name": "Artist"})
    original = asyncio.run(catalog.detail(artist["id"], "artist"))
    expire_artist(catalog, artist["id"])
    provider.page["albums"]["results"] = []
    provider.page["discography_complete"] = False
    partial = asyncio.run(catalog.detail(artist["id"], "artist"))
    assert {a["id"] for a in partial["album"]} == {
        a["id"] for a in original["album"]
    }
    assert catalog.read(artist["id"])["snapshot"]["complete"] is False
    assert asyncio.run(catalog.detail(artist["id"], "artist")) == partial
    assert len(provider.calls) == 2
    expire_artist(catalog, artist["id"])
    provider.page["discography_complete"] = True
    refreshed = asyncio.run(catalog.detail(artist["id"], "artist"))
    assert refreshed["albumCount"] == 2  # confirmed upstream removals


def test_concurrent_artist_reads_share_one_fetch(catalog_service):
    catalog = catalog_service[1].state.services.catalog
    provider = ArtistProvider()
    artist = catalog.artist({"id": ARTIST, "name": "Artist"})

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch(payload):
            started.set()
            await release.wait()
            return await provider.fetch(payload)

        catalog.provider = SimpleNamespace(fetch=fetch)
        first = asyncio.create_task(catalog.detail(artist["id"], "artist"))
        await started.wait()
        second = asyncio.create_task(catalog.detail(artist["id"], "artist"))
        release.set()
        return await asyncio.gather(first, second)

    results = asyncio.run(scenario())
    assert results[0] == results[1]
    assert len(provider.calls) == 1


def test_release_metadata_survives_album_hydration(catalog_service):
    catalog = catalog_service[1].state.services.catalog
    artist = search(catalog_service)["artist"][0]
    get(catalog_service, "getArtist", id=artist["id"])
    album = catalog.read_source(ALBUM, "album")["document"]
    assert album["releaseTypes"] == ["album"]
    hydrated = body(get(catalog_service, "getAlbum", id=album["id"]))["album"]
    assert hydrated["releaseTypes"] == ["album"]
    assert hydrated["releaseDate"] == {"year": 2013}
    assert hydrated["songCount"] == 2
    expire_artist(catalog, artist["id"])
    refreshed = body(get(catalog_service, "getArtist", id=artist["id"]))[
        "artist"
    ]
    assert all("song" not in a for a in refreshed["album"])
