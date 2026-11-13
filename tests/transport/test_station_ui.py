from support.accounts import csrf, login
from support.stations import STATION


def test_unlinked_admin_can_manage_stations(service):
    client, app, _ = service
    login(client)
    page = client.get("/admin")
    assert "Internet radio stations" in page.text
    token = csrf(page)
    response = client.post("/admin/stations", data=STATION | {"csrf": token})
    assert response.status_code == 200
    assert "Ambient &lt;&amp;&gt;" in response.text
    stations = app.state.services.stations
    station = stations.list()[0]
    response = client.post(
        f"/admin/stations/{station['id']}/save",
        data=STATION | {"csrf": token, "name": "Evening radio"},
    )
    assert response.status_code == 200
    assert stations.list()[0]["name"] == "Evening radio"
    response = client.post(
        f"/admin/stations/{station['id']}/save",
        data=STATION | {"csrf": token, "streamUrl": "file:///private"},
    )
    assert response.status_code == 400
    assert stations.list()[0]["name"] == "Evening radio"
    response = client.post(
        f"/admin/stations/{station['id']}/delete", data={"csrf": token}
    )
    assert response.status_code == 200
    assert stations.list() == []
    assert app.state.services.accounts.user("admin")["link_status"] == (
        "unlinked"
    )


def test_listener_cannot_manage_stations_in_browser(service):
    client, app, _ = service
    login(client, "alice")
    token = csrf(client.get("/"))
    for path in (
        "/admin/stations",
        "/admin/stations/example/save",
        "/admin/stations/example/delete",
        "/admin/charts/example/rename",
    ):
        response = client.post(path, data=STATION | {"csrf": token})
        assert response.status_code == 403
    assert app.state.services.stations.list() == []


def test_station_artwork_browser_edit_and_clear(service):
    client, app, _ = service
    login(client)
    page = client.get("/admin")
    token = csrf(page)
    artwork = "https://radio.example/logo.png?size=500&v=2"
    response = client.post(
        "/admin/stations",
        data=STATION
        | {
            "csrf": token,
            "artworkUrl": artwork,
        },
    )
    assert response.status_code == 200
    assert "Artwork URL (optional)" in response.text
    assert "https://radio.example/logo.png?size=500&amp;v=2" in response.text
    stations = app.state.services.stations
    station = stations.list()[0]
    assert station["artworkUrl"] == artwork
    response = client.post(
        f"/admin/stations/{station['id']}/save",
        data=STATION | {"csrf": token, "artworkUrl": ""},
    )
    assert response.status_code == 200
    assert "artworkUrl" not in stations.list()[0]
    assert "coverArt" not in stations.list()[0]
