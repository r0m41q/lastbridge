from support.build_accounts import make_accounts
from support.imports import accept, user

from lastbridge.features.playlists.adapters.sqlite.imports import (
    ImportRepository,
)
from lastbridge.platform.migrations.import_queue import SCHEMA


def test_schema_seventeen_upgrade_preserves_snapshots_names_and_accounts(
    imported_service,
):
    service, imports, _, _ = imported_service
    owner = user(service)
    chart = accept(imported_service, shared=True)
    private = accept(imported_service, name="alice")
    imports.rename_shared(owner, chart, "Custom chart")
    before = imports.get(owner, chart)
    database = service[1].state.services.database
    with database.transaction() as connection:
        connection.execute("DROP INDEX playlist_import_queue")
        for name in SCHEMA:
            connection.execute(
                f"ALTER TABLE playlist_imports DROP COLUMN {name}"
            )
        connection.execute("PRAGMA user_version=17")
    accounts = make_accounts(database.path.parent)
    repository = ImportRepository(accounts.repository.database)
    after = repository.get(owner, chart)
    for name in (
        "id",
        "source",
        "shared",
        "ready",
        "title",
        "custom_title",
        "entries",
        "created",
        "updated",
        "next_attempt",
    ):
        assert after[name] == before[name]
    assert after["state"] == "complete" and after["processed"] == 4
    assert after["total"] == 4 and after["matched"] == 3
    assert (
        repository.get(user(service, "alice"), private)["entries"]
        == (before["entries"])
    )
    with accounts.repository.database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 18
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == (
            "ok"
        )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    assert accounts.client_user({"apiKey": service[3]["admin"]})
