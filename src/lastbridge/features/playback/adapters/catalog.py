class CatalogArtwork:
    """Connect the catalog's artwork lookup to the playback-owned port."""

    def __init__(self, catalog, client):
        self.catalog, self.client = catalog, client

    async def fetch(self, source):
        song = self.catalog.read_source(source, "song")
        return await self.catalog.artwork(self.client, song["id"])
