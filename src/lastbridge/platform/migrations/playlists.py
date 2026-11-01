SCHEMA = (
    """CREATE TABLE playlists (
        id TEXT PRIMARY KEY, user_id INTEGER NOT NULL
            REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL, changed TEXT NOT NULL,
        downloaded INTEGER NOT NULL DEFAULT 0 CHECK(downloaded IN (0,1))
    )""",
    """CREATE INDEX playlists_owner ON playlists(user_id)""",
    """CREATE TABLE playlist_entries (
        playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        song_id TEXT NOT NULL REFERENCES catalog(id),
        PRIMARY KEY(playlist_id,position)
    )""",
    """CREATE VIEW retained_tracks AS
        SELECT user_id,song_id FROM favorite_tracks
        UNION
        SELECT p.user_id,e.song_id FROM playlists p
        JOIN playlist_entries e ON e.playlist_id=p.id WHERE p.downloaded=1
    """,
)
