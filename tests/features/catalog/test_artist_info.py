import time
from xml.etree.ElementTree import fromstring

import pytest
from support.artists import RankedMetadata
from support.catalog import ARTIST, body, get, search


@pytest.mark.parametrize(
    "method,field",
    [
        ("getArtistInfo", "artistInfo"),
        ("getArtistInfo2", "artistInfo2"),
    ],
)
def test_artist_info_ids_similar_parameters_json_xml_and_post(
    catalog_service,
    method,
    field,
):
    service = catalog_service
    catalog = service[1].state.services.catalog
    found = search(service)
    artist = found["artist"][0]
    metadata = RankedMetadata()
    catalog.extras.metadata = metadata
    metadata.biography = {
        "biography": "Artist &amp; friends.",
        "lastFmUrl": "https://www.last.fm/music/Artist",
        "musicBrainzId": "12345678-1234-1234-1234-123456789abc",
        "similar": ["Known", "Missing", "Known", "Ambiguous"],
    }
    known = catalog.artist({"id": "UC_known", "name": "Known"})
    for suffix in ("1", "2"):
        catalog.artist({"id": "UC_ambiguous" + suffix, "name": "Ambiguous"})
    info = body(get(service, method, id=artist["id"]))[field]
    assert [a["id"] for a in info["similarArtist"]] == [known["id"]]
    assert info["largeImageUrl"].endswith("=w1080-h1080-p-l90-rj")
    assert "apiKey" not in info["largeImageUrl"]
    assert info["biography"] == "Artist &amp; friends."
    assert info["musicBrainzId"] == metadata.biography["musicBrainzId"]
    for kind in ("album", "song"):
        assert (
            body(get(service, method, id=found[kind][0]["id"]))[field] == info
        )
    all_similar = body(
        get(service, method, id=artist["id"], includeNotPresent="true")
    )[field]["similarArtist"]
    assert [a["name"] for a in all_similar] == [
        "Known",
        "Missing",
        "Ambiguous",
    ]
    assert all_similar[1]["id"] == "-1"
    assert (
        len(
            body(get(service, method, id=artist["id"], count=1))[field][
                "similarArtist"
            ]
        )
        == 1
    )
    assert not body(get(service, method, id=artist["id"], count=0))[field][
        "similarArtist"
    ]
    xml = fromstring(get(service, method, id=artist["id"], f="xml").content)
    element = xml.find("{*}" + field)
    assert element.find("{*}biography").text == info["biography"]
    assert element.find("{*}largeImageUrl").text == info["largeImageUrl"]
    assert element.find("{*}similarArtist").attrib["id"] == known["id"]
    post = service[0].post(
        "/rest/" + method + ".view",
        data={
            "apiKey": service[3]["admin"],
            "v": "1.16.1",
            "c": "test",
            "f": "json",
            "id": artist["id"],
        },
    )
    assert body(post)[field] == info
    assert metadata.calls == [("info", artist["name"])]
    with catalog.repository.database.connection() as connection:
        for table in ("scrobbles", "save_jobs", "love_outbox"):
            assert (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[
                    0
                ]
                == 0
            )


def test_artist_info_head_outage_cache_and_private_stars(catalog_service):
    service = catalog_service
    catalog = service[1].state.services.catalog
    artist = search(service)["artist"][0]
    metadata = RankedMetadata()
    catalog.extras.metadata = metadata
    service[0].head(
        "/rest/getArtistInfo2",
        params={
            "apiKey": service[3]["admin"],
            "v": "1",
            "c": "test",
            "id": artist["id"],
        },
    )
    assert not metadata.calls
    known = catalog.artist({"id": "UC_known", "name": "Known"})
    metadata.biography["similar"] = ["Known"]
    original = body(get(service, "getArtistInfo2", id=artist["id"]))[
        "artistInfo2"
    ]
    catalog.extras.infos[ARTIST] = (time.monotonic() - 1, metadata.biography)
    metadata.fail = True
    for _ in range(2):
        assert (
            body(get(service, "getArtistInfo2", id=artist["id"]))[
                "artistInfo2"
            ]
            == original
        )
    assert len(metadata.calls) == 2
    get(service, "star", artistId=known["id"])
    own = body(get(service, "getArtistInfo2", id=artist["id"]))["artistInfo2"]
    other = body(
        get(
            service,
            "getArtistInfo2",
            id=artist["id"],
            apiKey=service[3]["alice"],
        )
    )["artistInfo2"]
    assert "starred" in own["similarArtist"][0]
    assert "starred" not in other["similarArtist"][0]


@pytest.mark.parametrize(
    "params,code",
    [
        ({}, 10),
        ({"id": "unknown"}, 70),
        ({"count": "bad"}, 10),
        ({"count": "-1"}, 10),
        ({"includeNotPresent": "yes"}, 10),
    ],
)
def test_artist_info_invalid_parameters(catalog_service, params, code):
    artist = search(catalog_service)["artist"][0]
    values = {"id": artist["id"], **params} if params else {}
    assert (
        body(get(catalog_service, "getArtistInfo2", **values))["error"]["code"]
        == code
    )


def test_artist_info_unconfigured_and_cold_outage_are_optional(
    catalog_service,
):
    service = catalog_service
    catalog = service[1].state.services.catalog
    assert catalog.extras.metadata.lastfm is service[1].state.services.lastfm
    assert (
        catalog.extras.metadata.client is service[1].state.services.http_client
    )
    artist = search(service)["artist"][0]
    info = body(get(service, "getArtistInfo2", id=artist["id"]))["artistInfo2"]
    assert info["largeImageUrl"] and "biography" not in info
    metadata = RankedMetadata()
    metadata.fail = True
    catalog.extras.metadata = metadata
    assert (
        body(get(service, "getArtistInfo2", id=artist["id"]))["artistInfo2"]
        == info
    )
