"""Outbound song-love intent on the favorite edit's transaction."""

from sqlite3 import Connection
from typing import Protocol


class LoveIntentWriter(Protocol):
    def record(
        self, connection: Connection, user_id: int, stored: dict, enabled: bool
    ): ...
