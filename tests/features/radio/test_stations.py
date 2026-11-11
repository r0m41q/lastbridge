from xml.etree.ElementTree import fromstring

import pytest
from support.catalog import body, get
from support.stations import STATION, station_call


@pytest.mark.parametrize("http_method", ["GET", "POST"])
def test_station_api_lifecycle_and_xml(catalog_service, http_method):
    service = catalog_service
    assert body(get(service, "getInternetRadioStations"))[
        "internetRadioStations"
    ] == {"internetRadioStation": []}
    result = station_call(
        service,
        "createInternetRadioStation.view",
        http_method=http_method,
        **STATION,
    )
    assert body(result)["status"] == "ok"
    stations = body(get(service, "getInternetRadioStations"))[
        "internetRadioStations"
    ]["internetRadioStation"]
    assert len(stations) == 1
    station = stations[0]
    assert station == {
        "id": station["id"],
        "name": STATION["name"],
        "streamUrl": STATION["streamUrl"],
        "homePageUrl": STATION["homepageUrl"],
    }
    xml = fromstring(get(service, "getInternetRadioStations", f="xml").content)
    entry = xml.find("{*}internetRadioStations/{*}internetRadioStation")
    assert entry.attrib == station
    listener = station_call(
        service, "getInternetRadioStations", account="unlinked"
    )
    assert body(listener)["internetRadioStations"]["internetRadioStation"]
    assert (
        body(get(service, "getPlaylists", apiKey=service[3]["unlinked"]))[
            "error"
        ]["code"]
        == 50
    )
    result = station_call(
        service,
        "updateInternetRadioStation",
        http_method=http_method,
        id=station["id"],
        name="Updated station",
        streamUrl="http://radio.example:8000/live.mp3",
    )
    assert body(result)["status"] == "ok"
    changed = service[1].state.services.stations.list()[0]
    assert changed == {
        "id": station["id"],
        "name": "Updated station",
        "streamUrl": "http://radio.example:8000/live.mp3",
    }
    result = station_call(
        service,
        "deleteInternetRadioStation",
        http_method=http_method,
        id=station["id"],
    )
    assert body(result)["status"] == "ok"
    assert service[1].state.services.stations.list() == []
    assert not service[2].calls


@pytest.mark.parametrize("account", ["alice", "unlinked"])
@pytest.mark.parametrize(
    "method",
    [
        "createInternetRadioStation",
        "updateInternetRadioStation",
        "deleteInternetRadioStation",
    ],
)
def test_only_administrators_can_mutate_stations(
    catalog_service, account, method
):
    response = station_call(catalog_service, method, account=account)
    assert body(response)["error"]["code"] == 50
    assert catalog_service[1].state.services.stations.list() == []


@pytest.mark.parametrize(
    "change",
    [
        {"name": " "},
        {"name": "x" * 201},
        {"name": "invalid\x00name"},
        {"name": "invalid\uffffname"},
        {"streamUrl": ""},
        {"streamUrl": "javascript:alert(1)"},
        {"streamUrl": "file:///etc/passwd"},
        {"streamUrl": "https://user:password@radio.example/stream"},
        {"streamUrl": "https://radio.example/stream#fragment"},
        {"streamUrl": "https://radio.example:99999/stream"},
        {"streamUrl": "https://radio.example:0/stream"},
        {"streamUrl": "https://radio.example/\nlive"},
        {"streamUrl": "https://[invalid/stream"},
        {"streamUrl": "https://radio.example\\evil/stream"},
        {"streamUrl": "https://radio.example/" + "x" * 2048},
        {"homepageUrl": "javascript:alert(1)"},
    ],
)
def test_invalid_station_fields_are_rejected(catalog_service, change):
    response = station_call(
        catalog_service, "createInternetRadioStation", **(STATION | change)
    )
    if "application/xml" in response.headers["content-type"]:
        assert fromstring(response.content).find("{*}error").get("code") == (
            "10"
        )
    else:
        assert body(response)["error"]["code"] == 10
    assert catalog_service[1].state.services.stations.list() == []


@pytest.mark.parametrize(
    "method,values,code",
    [
        ("createInternetRadioStation", {}, 10),
        ("updateInternetRadioStation", STATION, 10),
        ("updateInternetRadioStation", STATION | {"id": "missing"}, 70),
        ("deleteInternetRadioStation", {}, 10),
        ("deleteInternetRadioStation", {"id": "missing"}, 70),
    ],
)
def test_missing_station_parameters_and_ids(
    catalog_service, method, values, code
):
    response = station_call(catalog_service, method, **values)
    assert body(response)["error"]["code"] == code


@pytest.mark.parametrize(
    "method",
    [
        "createInternetRadioStation",
        "updateInternetRadioStation",
        "deleteInternetRadioStation",
    ],
)
def test_head_station_mutations_are_inert(catalog_service, method):
    station_call(catalog_service, "createInternetRadioStation", **STATION)
    stations = catalog_service[1].state.services.stations
    before = stations.list()
    response = station_call(
        catalog_service,
        method,
        http_method="HEAD",
        id=before[0]["id"],
        **(STATION | {"name": "Changed"}),
    )
    assert response.content == b""
    assert stations.list() == before


def test_station_authentication_without_lastfm(catalog_service):
    client, app, _, _ = catalog_service
    assert (
        body(
            client.get(
                "/rest/getInternetRadioStations",
                params={"v": "1.16.1", "c": "test", "f": "json"},
            )
        )["error"]["code"]
        == 40
    )
    with app.state.services.database.transaction() as connection:
        connection.execute(
            "UPDATE users SET link_status='unlinked' WHERE username='admin'"
        )
    response = station_call(
        catalog_service, "createInternetRadioStation", **STATION
    )
    assert body(response)["status"] == "ok"
    assert app.state.services.stations.list()
