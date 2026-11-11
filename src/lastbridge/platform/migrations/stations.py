SCHEMA = """CREATE TABLE IF NOT EXISTS internet_radio_stations (
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    stream_url TEXT NOT NULL, homepage_url TEXT NOT NULL DEFAULT ''
)"""
