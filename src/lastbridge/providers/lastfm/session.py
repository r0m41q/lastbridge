"""Bind Last.fm wire calls to a shared HTTP client."""


class LastfmSession:
    def __init__(self, lastfm, client):
        self.lastfm, self.client = lastfm, client

    @property
    def configured(self):
        return bool(self.lastfm and self.lastfm.configured and self.client)

    async def taste_rows(self, method, **parameters):
        return await self.lastfm.taste_rows(self.client, method, **parameters)

    async def loved(self, target, page):
        return await self.lastfm.loved(self.client, target, page)

    async def scrobble(self, secret, report):
        return await self.lastfm.scrobble(self.client, secret, report)
