import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LASTBRIDGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=5274, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = (
        "INFO"
    )
    data_dir: Path = Path("data")
    public_url: str = "http://127.0.0.1:5274"
    audio_cache_bytes: int = Field(default=1_000_000_000, ge=1_048_576)
    audio_file_max_bytes: int = Field(default=67_108_864, ge=1_048_576)
    audio_free_reserve_bytes: int = Field(default=268_435_456, ge=0)
    audio_policy: Literal["aac", "opus"] = "aac"
    mix_refresh_hours: int = Field(default=24, ge=1, le=8760)
    mix_inactive_days: int = Field(default=7, ge=1, le=3650)
    lastfm_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    lastfm_api_secret: SecretStr = Field(default=SecretStr(""), repr=False)

    @field_validator("lastfm_api_key", "lastfm_api_secret")
    @classmethod
    def validate_lastfm_credential(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if raw and not re.fullmatch(r"[a-fA-F0-9]{32}", raw):
            raise ValueError(
                "Last.fm credentials must contain 32 hex characters"
            )
        return value

    @model_validator(mode="after")
    def validate_lastfm_pair(self):
        if bool(self.lastfm_api_key.get_secret_value()) != bool(
            self.lastfm_api_secret.get_secret_value()
        ):
            raise ValueError("Set both Last.fm API key and API secret")
        return self

    @field_validator("public_url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("public_url must be an HTTP(S) origin")
        _ = parsed.port  # Validate the port without trusting proxy headers.
        return value.rstrip("/")
