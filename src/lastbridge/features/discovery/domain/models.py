"""A bounded local taste projection used by generation and list rendering."""

from typing import NamedTuple


class TasteSample(NamedTuple):
    saved: list[dict]
    recent: list[dict]
    loved: list[dict]
    known: set[str]
    removed: set[str]
    removed_ids: set[str]
