SCHEMA = """CREATE TABLE radio (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    target TEXT NOT NULL, seed_id TEXT NOT NULL REFERENCES catalog(id),
    songs TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
)"""
