SCHEMA = (
    """CREATE TABLE IF NOT EXISTS recording_corrections (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        song_id TEXT NOT NULL REFERENCES catalog(id),
        recording_id TEXT NOT NULL REFERENCES catalog(id),
        PRIMARY KEY(user_id,song_id)
    )""",
    """CREATE VIEW IF NOT EXISTS retained_audio AS
        SELECT t.user_id,t.song_id,
            COALESCE(r.recording_id,t.song_id) AS recording_id
        FROM retained_tracks t LEFT JOIN recording_corrections r
            ON r.user_id=t.user_id AND r.song_id=t.song_id
    """,
    """CREATE TABLE IF NOT EXISTS recording_reviews (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('song','love')),
        subject TEXT NOT NULL, token TEXT NOT NULL,
        fingerprint TEXT NOT NULL, candidates TEXT NOT NULL,
        expires REAL NOT NULL, PRIMARY KEY(user_id,kind,subject)
    )""",
)
