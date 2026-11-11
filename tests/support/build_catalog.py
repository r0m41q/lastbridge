"""Explicit test composition using the production service and adapters."""

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
from lastbridge.features.catalog.application.service import (
    Catalog as CatalogService,
)


def make_catalog(accounts, provider=None, artist_metadata=None):
    repository = CatalogRepository(
        accounts.repository.database, client_document
    )
    return CatalogService(
        repository,
        provider if provider is not None else MetadataProvider(),
        MetadataDocuments(repository),
        ArtworkLookup(repository),
        artist_metadata,
    )
