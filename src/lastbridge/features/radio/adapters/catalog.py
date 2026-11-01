"""Translate radio source lookups through the catalog's public identity."""

from lastbridge.features.catalog.domain.documents import identity


class RadioCatalog:
    def __init__(self, catalog):
        self.catalog = catalog

    def read(self, item_id, kind=None):
        return self.catalog.read(item_id, kind)

    def song_by_source(self, source):
        return self.read(identity("song", source), "song")["document"]

    async def detail(self, item_id, kind):
        return await self.catalog.detail(item_id, kind)

    async def radio(self, source):
        return await self.catalog.radio(source)

    async def match_loved(self, artist, title):
        return await self.catalog.match_loved(artist, title)
