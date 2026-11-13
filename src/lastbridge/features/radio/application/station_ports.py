from typing import Protocol


class StationStore(Protocol):
    def rows(self) -> list[dict]: ...

    def create(self, name: str, stream: str, homepage: str, artwork: str): ...

    def update(
        self,
        item_id: str,
        name: str,
        stream: str,
        homepage: str,
        artwork: str | None,
    ): ...
    def remove(self, item_id: str): ...
