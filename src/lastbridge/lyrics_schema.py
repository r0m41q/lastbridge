SCHEMA = """CREATE TABLE lyrics (
    song_id TEXT PRIMARY KEY REFERENCES catalog(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL, document TEXT NOT NULL,
    expires REAL NOT NULL, updated REAL NOT NULL
)"""
