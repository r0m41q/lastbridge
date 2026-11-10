from typing import Protocol


class CatalogAccess(Protocol):
    def read(self, item_id: str, kind: str | None = None) -> dict: ...
    def song_named(self, artist: str, title: str) -> str | None: ...


class LyricsStore(Protocol):
    def cached(self, song_id: str, fingerprint: str) -> dict | None: ...

    def save(
        self,
        song_id: str,
        fingerprint: str,
        document: list,
        ttl: float,
        updated: float | None = None,
    ): ...


class LyricsProvider(Protocol):
    async def get(self, song: dict) -> list[dict]: ...
