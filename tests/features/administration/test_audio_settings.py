import pytest
from support.build_accounts import make_accounts

from lastbridge.features.administration.adapters.sqlite.repository import (
    AudioSettingsRepository,
)
from lastbridge.features.administration.domain.audio import AudioSettings


def test_audio_settings_defaults_are_persisted(tmp_path):
    accounts = make_accounts(tmp_path)
    repository = AudioSettingsRepository(accounts.repository.database)

    assert repository.snapshot() == AudioSettings()


def test_audio_settings_round_trip(tmp_path):
    accounts = make_accounts(tmp_path)
    repository = AudioSettingsRepository(accounts.repository.database)
    settings = AudioSettings(
        policy="opus",
        cache_bytes=16 * 1024 * 1024,
        file_max_bytes=4 * 1024 * 1024,
        free_reserve_bytes=2 * 1024 * 1024,
        timeout_seconds=45,
    )

    repository.save(settings)

    assert repository.snapshot() == settings


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "unknown"},
        {"policy": "mp3"},
        {"jumo_base_url": "https://user:secret@example.invalid"},
        {"jumo_region": "USA"},
        {"cache_bytes": 1_000_000},
        {"file_max_bytes": 8 * 1024 * 1024, "cache_bytes": 4 * 1024 * 1024},
        {"free_reserve_bytes": -1},
        {"timeout_seconds": 601},
    ],
)
def test_audio_settings_reject_invalid_values(changes):
    values = AudioSettings().as_dict()
    values.update(changes)

    with pytest.raises(ValueError):
        AudioSettings(**values)


def test_audio_settings_reject_values_above_environment_ceilings():
    with pytest.raises(ValueError, match="deployment ceiling"):
        AudioSettings.from_form(
            {
                "provider": "youtube",
                "policy": "aac",
                "cache_bytes": str(16 * 1024 * 1024),
                "file_max_bytes": str(4 * 1024 * 1024),
                "free_reserve_bytes": "0",
                "timeout_seconds": "45",
                "jumo_base_url": "https://jumo-dl.pages.dev",
                "jumo_region": "",
                "jumo_quality": "original",
            },
            (8 * 1024 * 1024, 4 * 1024 * 1024, 4 * 1024 * 1024),
        )
