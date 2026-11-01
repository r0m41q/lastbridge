from pathlib import Path

from lastbridge.features.accounts.adapters.sqlite.repository import (
    AccountRepository,
)
from lastbridge.features.accounts.application.service import (
    Accounts as Service,
)
from lastbridge.features.accounts.domain.security import (
    SESSION_SECONDS,
    digest,
    password_hash,
    validate_account,
    verify_password,
)
from lastbridge.platform.credentials import Credentials
from lastbridge.platform.database import Database
from lastbridge.platform.migrations import migrate

__all__ = [
    "Accounts",
    "SESSION_SECONDS",
    "digest",
    "password_hash",
    "validate_account",
    "verify_password",
]


class Accounts(Service):
    def __init__(
        self, directory: Path | None = None, *, database=None, credentials=None
    ):
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            credentials = Credentials(
                directory, directory / "lastbridge.sqlite3"
            )
            database = Database(directory)
            migrate(database, credentials)
        if database is None or credentials is None:
            raise ValueError("Account storage dependencies are required")
        self.database = database
        super().__init__(AccountRepository(database), credentials)

    @property
    def path(self):
        return self.database.path

    def connection(self):
        return self.database.connection()

    def transaction(self):
        return self.database.transaction()
