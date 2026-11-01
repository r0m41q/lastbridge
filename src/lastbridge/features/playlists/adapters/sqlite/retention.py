"""Library retention writes on the playlist edit's existing transaction."""

from sqlite3 import Connection
from typing import Protocol


class RetentionWriter(Protocol):
    def schedule(self, connection: Connection): ...
