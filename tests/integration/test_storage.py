"""Storage failures cross the port without leaking driver dependencies."""

import sqlite3

import pytest

from lastbridge.platform.database import Database
from lastbridge.shared.errors import StorageError


def test_failed_transaction_rolls_back_before_mapping_driver_error(tmp_path):
    database = Database(tmp_path)
    with database.transaction() as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
    with pytest.raises(StorageError) as error:
        with database.transaction() as connection:
            connection.execute("INSERT INTO items VALUES(1)")
            connection.execute("INSERT INTO items VALUES(1)")
    assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
    with database.transaction() as connection:
        assert connection.execute("SELECT * FROM items").fetchall() == []
        connection.execute("INSERT INTO items VALUES(2)")
    with database.connection() as connection:
        assert connection.execute("SELECT id FROM items").fetchone()[0] == 2


def test_connection_failure_uses_same_storage_error(tmp_path):
    database = Database(tmp_path)
    database.path.unlink()
    database.path.mkdir()
    with pytest.raises(StorageError) as error:
        with database.connection():
            pytest.fail("A directory cannot be opened as a database")
    assert isinstance(error.value.__cause__, sqlite3.OperationalError)
