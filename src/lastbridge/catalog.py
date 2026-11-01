from lastbridge.features.catalog.adapters.http.artwork import ArtworkLookup
from lastbridge.features.catalog.adapters.sqlite.repository import (
    CatalogRepository,
)
from lastbridge.features.catalog.adapters.subsonic.documents import (
    client_document,
)
from lastbridge.features.catalog.adapters.youtube.documents import (
    MetadataDocuments,
)
from lastbridge.features.catalog.adapters.youtube.provider import (
    MetadataProvider,
)
from lastbridge.features.catalog.application.service import Catalog as Service
from lastbridge.features.catalog.domain.documents import (
    IMAGE_HOSTS,
    SOURCE,
    identity,
    image_url,
    number,
    thumbnail,
)
from lastbridge.shared.errors import CatalogError

__all__ = [
    "Catalog",
    "MetadataProvider",
    "IMAGE_HOSTS",
    "SOURCE",
    "client_document",
    "identity",
    "image_url",
    "number",
    "thumbnail",
    "CatalogError",
]


class Catalog(Service):
    def __init__(self, accounts, provider=None):
        repository = CatalogRepository(accounts.database, client_document)
        super().__init__(
            repository,
            provider if provider is not None else MetadataProvider(),
            MetadataDocuments(repository),
            ArtworkLookup(repository),
        )
