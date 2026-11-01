import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from lastbridge.shared.errors import StorageError


class Database:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "lastbridge.sqlite3"
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        try:
            connection = sqlite3.connect(self.path, timeout=5)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                yield connection
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise StorageError("Storage unavailable") from error

    @contextmanager
    def transaction(self):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                yield connection
