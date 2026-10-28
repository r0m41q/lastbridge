SCHEMA = (
    """CREATE TABLE playback_starts (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        song_id TEXT NOT NULL REFERENCES catalog(id), client TEXT NOT NULL,
        timestamp INTEGER NOT NULL, PRIMARY KEY(user_id,song_id,client)
    )""",
    """CREATE TABLE scrobbles (
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL
            REFERENCES users(id) ON DELETE CASCADE,
        target TEXT NOT NULL, song_id TEXT NOT NULL REFERENCES catalog(id),
        timestamp INTEGER NOT NULL, received INTEGER NOT NULL,
        artist TEXT NOT NULL, title TEXT NOT NULL, album TEXT NOT NULL,
        duration INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
            CHECK(state IN ('pending','running','sent','ignored','failed',
                            'blocked','uncertain')),
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0,
        error TEXT, UNIQUE(user_id,target,song_id,timestamp)
    )""",
    "CREATE INDEX scrobble_due ON scrobbles(state,next_attempt,timestamp)",
    """CREATE TABLE love_imports (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        target TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active'
            CHECK(state IN ('active','paused','complete')),
        next_page INTEGER NOT NULL DEFAULT 1, total_pages INTEGER,
        fetched INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0,
        error TEXT, revision INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE loved_items (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        target TEXT NOT NULL, recording_key TEXT NOT NULL,
        artist TEXT NOT NULL, title TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK(state IN ('pending','matched','unmatched','skipped')),
        song_id TEXT REFERENCES catalog(id), error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(user_id,target,recording_key)
    )""",
    """CREATE TABLE favorite_removals (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        item_id TEXT NOT NULL REFERENCES catalog(id),
        PRIMARY KEY(user_id,item_id)
    )""",
    """CREATE TABLE love_removals (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        recording_key TEXT NOT NULL, PRIMARY KEY(user_id,recording_key)
    )""",
)
