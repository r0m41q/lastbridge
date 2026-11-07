from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Literal

MIN_CACHE_BYTES = 2_097_152
MIN_FILE_BYTES = 1_048_576
MAX_TIMEOUT_SECONDS = 600
MIN_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class AudioSettings:
    provider: Literal["youtube"] = "youtube"
    policy: Literal["aac", "opus"] = "aac"
    cache_bytes: int = 1_000_000_000
    file_max_bytes: int = 67_108_864
    free_reserve_bytes: int = 268_435_456
    timeout_seconds: int = 180

    def __post_init__(self):
        if self.provider != "youtube":
            raise ValueError("Select a supported audio provider.")
        if self.policy not in ("aac", "opus"):
            raise ValueError("Select a supported audio format.")
        if self.cache_bytes < MIN_CACHE_BYTES:
            raise ValueError("The audio cache limit is too small.")
        if self.file_max_bytes < MIN_FILE_BYTES:
            raise ValueError("The audio file limit is too small.")
        if self.file_max_bytes > self.cache_bytes // 2:
            raise ValueError(
                "The audio file limit must not exceed half of the cache limit."
            )
        if self.free_reserve_bytes < 0:
            raise ValueError("The free-space reserve cannot be negative.")
        if not (
            MIN_TIMEOUT_SECONDS
            <= self.timeout_seconds
            <= MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "The provider timeout must be between 5 and 600 seconds."
            )

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)

    @classmethod
    def from_form(cls, values: Mapping[str, str]) -> AudioSettings:
        def number(name: str) -> int:
            raw = values.get(name, "")
            try:
                return int(raw)
            except (TypeError, ValueError):
                raise ValueError(
                    "Audio settings must use whole numbers."
                ) from None

        return cls(
            provider=values.get("provider", ""),
            policy=values.get("policy", ""),
            cache_bytes=number("cache_bytes"),
            file_max_bytes=number("file_max_bytes"),
            free_reserve_bytes=number("free_reserve_bytes"),
            timeout_seconds=number("timeout_seconds"),
        )
