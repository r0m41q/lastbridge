from lastbridge.features.library.adapters.sqlite.favorites import (
    FavoriteRepository,
)
from lastbridge.features.library.adapters.sqlite.history import (
    HistoryRepository,
)
from lastbridge.features.library.adapters.sqlite.jobs import JobRepository


class LibraryRepository(FavoriteRepository, JobRepository, HistoryRepository):
    """Shared transaction owner for favorite intents and durable save jobs."""
