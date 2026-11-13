from support.imports import accept, user


def test_schema_nine_upgrade_and_backup_restore_preserve_existing_state(
    imported_service,
    tmp_path,
):
    import shutil

    from support.build_accounts import make_accounts

    from lastbridge.features.playlists.adapters.sqlite.imports import (
        ImportRepository,
    )

    service, imports, _, _ = imported_service
    account = service[1].state.services.accounts
    directory = service[1].state.services.database.path.parent
    key = (directory / "credentials.key").read_bytes()
    # Simulate the previous schema, with actual catalog/account state present.
    with account.repository.database.transaction() as connection:
        connection.execute("DROP TABLE playlist_imports")
        connection.execute("DROP TABLE media_files")
        connection.execute("PRAGMA user_version=9")
        tables = [
            r[0]
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name"
            )
        ]
        before = {
            table: [
                tuple(r)
                for r in connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                )
            ]
            for table in tables
        }
    migrated = make_accounts(directory)
    with migrated.repository.database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 24
        assert (
            connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        after = {
            table: [
                tuple(r)
                for r in connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                )
            ]
            for table in tables
        }
    assert after == before
    assert (directory / "credentials.key").read_bytes() == key
    item_id = accept(imported_service, shared=True)
    backup = tmp_path / "backup"
    shutil.copytree(directory, backup)
    restored = make_accounts(backup)
    repository = ImportRepository(restored.repository.database)
    assert (
        repository.get(restored.user("admin"), item_id)["entries"]
        == imports.get(
            user(service),
            item_id,
        )["entries"]
    )
    assert restored.client_user({"apiKey": service[3]["admin"]})
    assert (backup / "credentials.key").read_bytes() == key


def test_schema_23_chart_schedule_upgrade_and_restart(imported_service):
    from support.build_accounts import make_accounts

    from lastbridge.features.playlists.adapters.sqlite.imports import (
        ImportRepository,
    )

    service, imports, _, _ = imported_service
    chart = accept(imported_service, shared=True)
    owner = user(service)
    before = imports.get(owner, chart)
    database = service[1].state.services.database
    with database.transaction() as connection:
        connection.execute(
            "ALTER TABLE playlist_imports DROP COLUMN refresh_days"
        )
        connection.execute(
            "ALTER TABLE playlist_imports DROP COLUMN last_checked"
        )
        connection.execute("PRAGMA user_version=23")
    migrated = make_accounts(database.path.parent)
    repository = ImportRepository(migrated.repository.database)
    after = repository.get(owner, chart)
    assert after["entries"] == before["entries"]
    assert after["updated"] == before["updated"]
    assert after["next_attempt"] == before["next_attempt"]
    assert after["last_checked"] == before["updated"]
    assert after["refresh_days"] == 1
    repository.schedule_shared(chart, 7)
    restarted = make_accounts(database.path.parent)
    restored = ImportRepository(restarted.repository.database).get(
        owner, chart
    )
    assert restored["refresh_days"] == 7
    assert restored["next_attempt"] == after["last_checked"] + 7 * 86400
