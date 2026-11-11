import json

from lastbridge.platform.migrations.audio import (
    DEFAULTS as AUDIO_DEFAULTS,
)
from lastbridge.platform.migrations.audio import (
    SCHEMA as AUDIO_SCHEMA,
)
from lastbridge.platform.migrations.discovery import SCHEMA as MIX_SCHEMA
from lastbridge.platform.migrations.import_queue import (
    SCHEMA as IMPORT_QUEUE_SCHEMA,
)
from lastbridge.platform.migrations.imports import SCHEMA as IMPORT_SCHEMA
from lastbridge.platform.migrations.loves import SCHEMA as LOVE_SCHEMA
from lastbridge.platform.migrations.lyrics import SCHEMA as LYRICS_SCHEMA
from lastbridge.platform.migrations.media import SCHEMA as MEDIA_SCHEMA
from lastbridge.platform.migrations.playlists import SCHEMA as PLAYLIST_SCHEMA
from lastbridge.platform.migrations.radio import SCHEMA as RADIO_SCHEMA
from lastbridge.platform.migrations.stations import SCHEMA as STATION_SCHEMA
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
        if version > 18:
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
        if version < 10:
            for statement in IMPORT_SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version=10")
        if version < 11:
            connection.execute(MEDIA_SCHEMA)
            connection.execute("PRAGMA user_version=11")
        if version < 12:
            connection.execute(AUDIO_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO audio_settings "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                AUDIO_DEFAULTS,
            )
            connection.execute("PRAGMA user_version=12")
        if version < 13:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(audio_settings)"
                )
            }
            if "youtube_fallback" not in columns:
                connection.execute(
                    "ALTER TABLE audio_settings RENAME TO audio_settings_old"
                )
                connection.execute(AUDIO_SCHEMA)
                connection.execute(
                    "INSERT INTO audio_settings "
                    "(id,provider,stream_provider,download_provider,policy,"
                    "cache_bytes,file_max_bytes,free_reserve_bytes,"
                    "timeout_seconds,youtube_fallback,jumo_base_url,"
                    "jumo_region,jumo_quality) "
                    "SELECT id,provider,provider,provider,policy,"
                    "cache_bytes,file_max_bytes,free_reserve_bytes,"
                    "timeout_seconds,1,"
                    "'https://jumo-dl.pages.dev','','original' "
                    "FROM audio_settings_old"
                )
                connection.execute("DROP TABLE audio_settings_old")
            connection.execute("PRAGMA user_version=13")
        if version < 14:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(audio_settings)"
                )
            }
            if "stream_provider" not in columns:
                connection.execute(
                    "ALTER TABLE audio_settings ADD COLUMN "
                    "stream_provider TEXT NOT NULL DEFAULT 'youtube' "
                    "CHECK(stream_provider IN ('youtube','jumo'))"
                )
                connection.execute(
                    "ALTER TABLE audio_settings ADD COLUMN "
                    "download_provider TEXT NOT NULL DEFAULT 'youtube' "
                    "CHECK(download_provider IN ('youtube','jumo'))"
                )
                connection.execute(
                    "UPDATE audio_settings SET stream_provider=provider,"
                    "download_provider=provider"
                )
            connection.execute("PRAGMA user_version=14")
        if version < 15:
            connection.execute(
                "ALTER TABLE audio_settings RENAME TO audio_settings_old"
            )
            connection.execute(AUDIO_SCHEMA)
            connection.execute(
                "INSERT INTO audio_settings ("
                "id,provider,stream_provider,download_provider,policy,"
                "cache_bytes,file_max_bytes,free_reserve_bytes,"
                "timeout_seconds,youtube_fallback,jumo_base_url,"
                "jumo_region,jumo_quality) SELECT "
                "id,provider,stream_provider,download_provider,policy,"
                "cache_bytes,file_max_bytes,free_reserve_bytes,"
                "timeout_seconds,youtube_fallback,jumo_base_url,"
                "jumo_region,jumo_quality FROM audio_settings_old"
            )
            connection.execute("DROP TABLE audio_settings_old")
            connection.execute("PRAGMA user_version=15")
        if version < 16:
            for statement in LOVE_SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version=16")
        if version < 17:
            connection.execute(STATION_SCHEMA)
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(playlist_imports)"
                )
            }
            if "custom_title" not in columns:
                connection.execute(
                    "ALTER TABLE playlist_imports ADD COLUMN custom_title TEXT"
                )
            connection.execute("PRAGMA user_version=17")
        if version < 18:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(playlist_imports)"
                )
            }
            for name, definition in IMPORT_QUEUE_SCHEMA.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE playlist_imports "
                        f"ADD COLUMN {name} {definition}"
                    )
            if "state" not in columns:
                for row in connection.execute(
                    "SELECT id,entries,created FROM playlist_imports"
                ).fetchall():
                    entries = json.loads(row["entries"])
                    connection.execute(
                        "UPDATE playlist_imports SET processed=?,total=?,"
                        "matched=?,queued_at=? WHERE id=?",
                        (
                            len(entries),
                            len(entries),
                            sum(bool(e.get("song_id")) for e in entries),
                            row["created"],
                            row["id"],
                        ),
                    )
            connection.execute(
                "UPDATE playlist_imports SET token=NULL WHERE state='complete'"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS playlist_import_queue ON "
                "playlist_imports(state,next_attempt,queued_at)"
            )
            connection.execute("PRAGMA user_version=18")
        check = connection.execute(
            "SELECT value FROM credential_check"
        ).fetchone()[0]
        if credentials.decrypt(check) != "lastbridge":
            raise RuntimeError("Credential key does not match database")
