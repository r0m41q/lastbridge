SCHEMA = """CREATE TABLE IF NOT EXISTS audio_settings (
    id INTEGER PRIMARY KEY CHECK(id=1),
    provider TEXT NOT NULL CHECK(provider IN ('youtube','jumo','rhythmax')),
    stream_provider TEXT NOT NULL DEFAULT 'youtube'
        CHECK(stream_provider IN ('youtube','jumo','rhythmax')),
    download_provider TEXT NOT NULL DEFAULT 'youtube'
        CHECK(download_provider IN ('youtube','jumo','rhythmax')),
    policy TEXT NOT NULL CHECK(policy IN ('aac','opus')),
    cache_bytes INTEGER NOT NULL CHECK(cache_bytes >= 2097152),
    file_max_bytes INTEGER NOT NULL CHECK(file_max_bytes >= 1048576),
    free_reserve_bytes INTEGER NOT NULL CHECK(free_reserve_bytes >= 0),
    timeout_seconds INTEGER NOT NULL CHECK(timeout_seconds BETWEEN 5 AND 600),
    youtube_fallback INTEGER NOT NULL CHECK(youtube_fallback IN (0,1)),
    jumo_base_url TEXT NOT NULL,
    jumo_region TEXT NOT NULL,
    jumo_quality TEXT NOT NULL CHECK(jumo_quality IN ('original','cd'))
)"""

DEFAULTS = (
    1,
    "youtube",
    "youtube",
    "youtube",
    "aac",
    1_000_000_000,
    67_108_864,
    268_435_456,
    180,
    1,
    "https://jumo-dl.pages.dev",
    "",
    "original",
)
