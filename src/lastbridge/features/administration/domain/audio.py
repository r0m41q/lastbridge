import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Literal
from urllib.parse import urlsplit

MIN_CACHE_BYTES = 2_097_152
MIN_FILE_BYTES = 1_048_576
MAX_TIMEOUT_SECONDS = 600
MIN_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class AudioSettings:
    provider: Literal["youtube", "jumo"] = "youtube"
    policy: Literal["aac", "opus"] = "aac"
    cache_bytes: int = 1_000_000_000
    file_max_bytes: int = 67_108_864
    free_reserve_bytes: int = 268_435_456
    timeout_seconds: int = 180
    youtube_fallback: bool = True
    jumo_base_url: str = "https://jumo-dl.pages.dev"
    jumo_region: str = ""
    jumo_quality: Literal["original", "cd"] = "original"

    def __post_init__(self):
        if self.provider not in ("youtube", "jumo"):
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
            MIN_TIMEOUT_SECONDS <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "The provider timeout must be between 5 and 600 seconds."
            )
        parsed = urlsplit(self.jumo_base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("The Jumo URL must be an HTTP(S) origin.")
        if self.jumo_region and not re.fullmatch(
            r"[A-Z]{2}", self.jumo_region
        ):
            raise ValueError("The Jumo region must be a two-letter code.")
        if self.jumo_quality not in ("original", "cd"):
            raise ValueError("Select a supported Jumo quality.")

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)

    @classmethod
    def from_form(
        cls,
        values: Mapping[str, str],
        ceilings: tuple[int, int, int] | None = None,
    ) -> AudioSettings:
        def number(name: str) -> int:
            raw = values.get(name, "")
            try:
                return int(raw)
            except TypeError, ValueError:
                raise ValueError(
                    "Audio settings must use whole numbers."
                ) from None

        settings = cls(
            provider=values.get("provider", ""),
            policy=values.get("policy", ""),
            cache_bytes=number("cache_bytes"),
            file_max_bytes=number("file_max_bytes"),
            free_reserve_bytes=number("free_reserve_bytes"),
            timeout_seconds=number("timeout_seconds"),
            youtube_fallback=values.get("youtube_fallback") == "yes",
            jumo_base_url=values.get("jumo_base_url", ""),
            jumo_region=values.get("jumo_region", "").strip().upper(),
            jumo_quality=values.get("jumo_quality", ""),
        )
        if ceilings is not None:
            for name, value, ceiling in (
                ("cache", settings.cache_bytes, ceilings[0]),
                ("file", settings.file_max_bytes, ceilings[1]),
                ("free-space", settings.free_reserve_bytes, ceilings[2]),
            ):
                if value > ceiling:
                    raise ValueError(
                        f"The audio {name} setting exceeds its "
                        "deployment ceiling."
                    )
        return settings

    def within_ceilings(self, ceilings: tuple[int, int, int]) -> AudioSettings:
        cache = min(self.cache_bytes, ceilings[0])
        maximum = min(self.file_max_bytes, ceilings[1], cache // 2)
        reserve = min(self.free_reserve_bytes, ceilings[2])
        return AudioSettings(
            provider=self.provider,
            policy=self.policy,
            cache_bytes=cache,
            file_max_bytes=maximum,
            free_reserve_bytes=reserve,
            timeout_seconds=self.timeout_seconds,
            youtube_fallback=self.youtube_fallback,
            jumo_base_url=self.jumo_base_url,
            jumo_region=self.jumo_region,
            jumo_quality=self.jumo_quality,
        )
