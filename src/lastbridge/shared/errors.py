class CatalogError(Exception):
    def __init__(self, code=0, message="Metadata provider unavailable"):
        self.code, self.message = code, message
        super().__init__(message)


class ProviderError(Exception):
    """Only fixed public codes cross provider boundaries."""


class StorageError(Exception):
    """A persistence operation failed; adapters retain the original cause."""
