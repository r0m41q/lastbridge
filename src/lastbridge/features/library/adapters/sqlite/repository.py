from lastbridge.features.library.adapters.sqlite.favorites import (
    FavoriteRepository,
)
from lastbridge.features.library.adapters.sqlite.jobs import JobRepository


class LibraryRepository(FavoriteRepository, JobRepository):
    """Shared transaction owner for favorite intents and durable save jobs."""
