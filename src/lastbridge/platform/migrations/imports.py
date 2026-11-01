SCHEMA = (
    """CREATE TABLE playlist_imports (
        id TEXT PRIMARY KEY,
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        source TEXT NOT NULL, shared INTEGER NOT NULL,
        ready INTEGER NOT NULL DEFAULT 0,
        title TEXT NOT NULL, entries TEXT NOT NULL,
        created REAL NOT NULL, updated REAL NOT NULL,
        next_attempt REAL NOT NULL, token TEXT, error TEXT
    )""",
    "CREATE UNIQUE INDEX shared_playlist_source ON playlist_imports(source) "
    "WHERE shared=1 AND ready=1",
    "CREATE UNIQUE INDEX private_playlist_source ON "
    "playlist_imports(owner_id,source) WHERE shared=0 AND ready=1",
)
