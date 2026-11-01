from lastbridge.platform.migrations.discovery import SCHEMA as MIX_SCHEMA
from lastbridge.platform.migrations.lyrics import SCHEMA as LYRICS_SCHEMA
from lastbridge.platform.migrations.playlists import SCHEMA as PLAYLIST_SCHEMA
from lastbridge.platform.migrations.radio import SCHEMA as RADIO_SCHEMA
from lastbridge.platform.migrations.synchronization import (
    SCHEMA as SYNC_SCHEMA,
)

SCHEMA = (
    "CREATE TABLE credential_check (value TEXT NOT NULL)",
    """CREATE TABLE users (
        id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0,
        client_secret TEXT, client_lookup TEXT UNIQUE,
        lastfm_name TEXT, lastfm_secret TEXT,
        link_status TEXT NOT NULL DEFAULT 'unlinked'
            CHECK(link_status IN ('unlinked','linked','relink'))
    )""",
    """CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL
            REFERENCES users(id) ON DELETE CASCADE,
        csrf TEXT NOT NULL, expires INTEGER NOT NULL
    )""",
    """CREATE TABLE link_flows (
        state_hash TEXT PRIMARY KEY, session_hash TEXT NOT NULL
            REFERENCES sessions(token_hash) ON DELETE CASCADE,
        expires INTEGER NOT NULL
    )""",
    "CREATE TABLE used_link_tokens (token_hash TEXT PRIMARY KEY)",
    """CREATE TABLE login_limits (
        scope TEXT PRIMARY KEY, attempts INTEGER NOT NULL,
        expires INTEGER NOT NULL
    )""",
)


def migrate(database, credentials):
    with database.transaction() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 9:
            raise RuntimeError("Database is newer than this application")
        if version == 0:
            for statement in SCHEMA:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO credential_check VALUES(?)",
                (credentials.encrypt("lastbridge"),),
            )
            connection.execute("PRAGMA user_version=1")
        if version < 2:
            connection.execute(
                "CREATE TABLE catalog (id TEXT PRIMARY KEY, kind TEXT "
                "NOT NULL CHECK(kind IN ('artist','album','song')), "
                "source TEXT NOT NULL, document TEXT NOT NULL, "
                "image TEXT, "
                "complete INTEGER NOT NULL DEFAULT 0, updated INTEGER "
                "NOT NULL, UNIQUE(kind,source))"
            )
            connection.execute("PRAGMA user_version=2")
        if version < 3:
            connection.execute(
                "ALTER TABLE users ADD COLUMN library_changed "
                "INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute(
                "CREATE TABLE favorites (user_id INTEGER NOT NULL "
                "REFERENCES users(id) ON DELETE CASCADE, item_id TEXT "
                "NOT NULL REFERENCES catalog(id), starred TEXT NOT NULL, "
                "PRIMARY KEY(user_id,item_id))"
            )
            connection.execute(
                "CREATE TABLE favorite_tracks (user_id INTEGER NOT NULL, "
                "favorite_id TEXT NOT NULL, song_id TEXT NOT NULL "
                "REFERENCES catalog(id), "
                "PRIMARY KEY(user_id,favorite_id,song_id), "
                "FOREIGN KEY(user_id,favorite_id) REFERENCES "
                "favorites(user_id,item_id) ON DELETE CASCADE)"
            )
            connection.execute(
                "CREATE TABLE save_jobs (source TEXT NOT NULL, policy "
                "TEXT NOT NULL CHECK(policy IN ('aac','opus')), state "
                "TEXT NOT NULL CHECK(state IN "
                "('pending','running','failed','complete')), attempts "
                "INTEGER NOT NULL DEFAULT 0, error TEXT, next_attempt "
                "REAL NOT NULL DEFAULT 0, PRIMARY KEY(source,policy))"
            )
            connection.execute("PRAGMA user_version=3")
        if version < 4:
            for statement in SYNC_SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version=4")
        if version < 5:
            connection.execute(MIX_SCHEMA)
            connection.execute("PRAGMA user_version=5")
        if version < 6:
            connection.execute(RADIO_SCHEMA)
            connection.execute("PRAGMA user_version=6")
        if version < 7:
            connection.execute(LYRICS_SCHEMA)
            connection.execute("PRAGMA user_version=7")
        if version < 8:
            for statement in PLAYLIST_SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version=8")
        if version < 9:
            connection.execute(
                "CREATE TABLE mix_activity (user_id INTEGER PRIMARY KEY "
                "REFERENCES users(id) ON DELETE CASCADE, "
                "last_active REAL NOT NULL)"
            )
            connection.execute("PRAGMA user_version=9")
        check = connection.execute(
            "SELECT value FROM credential_check"
        ).fetchone()[0]
        if credentials.decrypt(check) != "lastbridge":
            raise RuntimeError("Credential key does not match database")
