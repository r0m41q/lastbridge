from support.build_accounts import make_accounts
from support.imports import accept, user
from support.library import run
from support.stations import STATION

from lastbridge.features.playlists.adapters.sqlite.imports import (
    ImportRepository,
)
from lastbridge.features.radio.adapters.sqlite.stations import (
    StationRepository,
)
from lastbridge.features.radio.application.stations import Stations


def test_version_sixteen_upgrade_and_station_chart_restart(imported_service):
    service, imports, state, _ = imported_service
    chart = accept(imported_service, shared=True)
    private = accept(imported_service)
    owner = user(service)
    before = imports.get(owner, chart)
    database = service[1].state.services.database
    with database.transaction() as connection:
        connection.execute("DROP TABLE internet_radio_stations")
        connection.execute(
            "ALTER TABLE playlist_imports DROP COLUMN custom_title"
        )
        connection.execute("PRAGMA user_version=16")
    migrated = make_accounts(database.path.parent)
    with migrated.repository.database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 19
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == (
            "ok"
        )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    assert imports.get(owner, chart) == before
    assert imports.get(owner, private)["custom_title"] is None
    imports.rename_shared(owner, chart, "Persistent chart")
    stations = service[1].state.services.stations
    stations.save(owner, STATION)
    station = stations.list()[0]
    restarted = make_accounts(database.path.parent)
    store = restarted.repository.database
    assert Stations(StationRepository(store)).list() == [station]
    restored_imports = ImportRepository(store)
    assert restored_imports.get(owner, chart)["title"] == "Persistent chart"
    state["title"] = "Changed source title"
    job = restored_imports.claim(chart)
    restored_imports.publish(
        job, run(service, imports.provider.load, job["source"])
    )
    assert imports.get(owner, chart)["title"] == "Persistent chart"
    assert imports.get(owner, private)["title"] == before["title"]
