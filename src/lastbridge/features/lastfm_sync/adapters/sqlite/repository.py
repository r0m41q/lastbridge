from lastbridge.features.lastfm_sync.adapters.sqlite.delivery import (
    DeliveryRepository,
)
from lastbridge.features.lastfm_sync.adapters.sqlite.imports import (
    ImportRepository,
)
from lastbridge.features.lastfm_sync.adapters.sqlite.reports import (
    ReportRepository,
)


class SyncRepository(ReportRepository, DeliveryRepository, ImportRepository):
    def __init__(self, database):
        self.database = database
