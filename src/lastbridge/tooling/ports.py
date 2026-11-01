"""Provider contract for the opt-in developer probe."""

from pathlib import Path
from typing import Any, Protocol

from lastbridge.features.catalog.domain.models import Candidate, Recording


class MusicProvider(Protocol):
    def search(self, recording: Recording) -> list[Candidate]: ...

    def inspect(
        self, source_id: str, sample_dir: Path | None = None
    ) -> dict[str, Any]: ...
