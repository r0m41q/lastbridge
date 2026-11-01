"""User exclusions; a start remains distinct from completed listening."""

from typing import NamedTuple


class Exclusions(NamedTuple):
    recent_ids: set[str]
    removed_recordings: set[str]
    excluded_ids: set[str]
