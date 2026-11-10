SCHEMA = (
    """CREATE TABLE IF NOT EXISTS love_outbox (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        target TEXT NOT NULL, recording_key TEXT NOT NULL,
        artist TEXT NOT NULL, title TEXT NOT NULL,
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 1,
        updated REAL NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK(state IN ('pending','running','sent','failed','blocked')),
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, error TEXT,
        UNIQUE(user_id,target,recording_key)
    )""",
    "CREATE INDEX IF NOT EXISTS love_due "
    "ON love_outbox(state,next_attempt,id)",
)
