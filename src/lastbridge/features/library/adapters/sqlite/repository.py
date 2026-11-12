from lastbridge.features.library.adapters.sqlite.corrections import (
    CorrectionRepository,
)
from lastbridge.features.library.adapters.sqlite.favorites import (
    FavoriteRepository,
)
from lastbridge.features.library.adapters.sqlite.history import (
    HistoryRepository,
)
from lastbridge.features.library.adapters.sqlite.jobs import JobRepository


class LibraryRepository(
    FavoriteRepository, JobRepository, HistoryRepository, CorrectionRepository
):
    """Shared transaction owner for favorite intents and durable save jobs."""
