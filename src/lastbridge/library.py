from lastbridge.features.library.adapters.sqlite.repository import (
    LibraryRepository,
)
from lastbridge.features.library.application.service import Library as Service


class Library(Service):
    def __init__(self, accounts, catalog, policy):
        super().__init__(
            LibraryRepository(accounts.database, policy), catalog, policy
        )
