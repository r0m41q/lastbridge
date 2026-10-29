import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

from lastbridge.lyrics_schema import SCHEMA as LYRICS_SCHEMA
from lastbridge.mix_schema import SCHEMA as MIX_SCHEMA
from lastbridge.playlist_schema import SCHEMA as PLAYLIST_SCHEMA
from lastbridge.radio_schema import SCHEMA as RADIO_SCHEMA
from lastbridge.sync_schema import SCHEMA as SYNC_SCHEMA

USERNAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
SESSION_SECONDS = 12 * 60 * 60


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    value = hashlib.scrypt(
        password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=67108864
    )
    return f"{salt.hex()}:{value.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    salt = bytes.fromhex(encoded.split(":")[0])
    return hmac.compare_digest(password_hash(password, salt), encoded)


def validate_account(username: str, password: str) -> None:
    if not USERNAME.fullmatch(username):
        raise ValueError("Use 1–64 lowercase letters, digits, dots, - or _.")
    if not 12 <= len(password) <= 256:
        raise ValueError("Browser passwords must contain 12–256 characters.")


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


class Accounts:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "lastbridge.sqlite3"
        key_path = directory / "credentials.key"
        # A missing key on an existing database is a recovery problem, not a
        # reason to silently replace the key and lose encrypted credentials.
        if not key_path.exists():
            if self.path.exists():
                raise RuntimeError(
                    "Credential key missing; restore from backup"
                )
            try:
                descriptor = os.open(
                    key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as destination:
                    destination.write(Fernet.generate_key())
        key_path.chmod(0o600)
        self.cipher = Fernet(key_path.read_bytes())
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        with self.transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 8:
                raise RuntimeError("Database is newer than this application")
            if version == 0:
                for statement in SCHEMA:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO credential_check VALUES(?)",
                    (self.encrypt("lastbridge"),),
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
            check = connection.execute(
                "SELECT value FROM credential_check"
            ).fetchone()[0]
            if self.decrypt(check) != "lastbridge":
                raise RuntimeError("Credential key does not match database")
        # Constant-cost missing-user login verification, generated once/start.
        self.dummy_password = password_hash(secrets.token_urlsafe(32))

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                yield connection

    def encrypt(self, value: str) -> str:
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self.cipher.decrypt(value.encode()).decode()

    def user(self, username: str):
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM users WHERE username=?", (username,)
            ).fetchone()

    def create_user(
        self, username: str, password: str, *, bootstrap: bool = False
    ) -> None:
        validate_account(username, password)
        encoded = password_hash(password)
        with self.transaction() as connection:
            if (
                bootstrap
                and connection.execute(
                    "SELECT 1 FROM users LIMIT 1"
                ).fetchone()
            ):
                raise ValueError(
                    "Administrator bootstrap is already complete."
                )
            try:
                connection.execute(
                    "INSERT INTO users(username,password_hash,admin) "
                    "VALUES(?,?,?)",
                    (username, encoded, int(bootstrap)),
                )
            except sqlite3.IntegrityError:
                raise ValueError("Username is already in use.") from None

    def reset_password(self, username: str, password: str) -> None:
        validate_account(username, password)
        encoded = password_hash(password)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM users WHERE username=?", (username,)
            ).fetchone()
            if row is None:
                raise ValueError("Account does not exist.")
            connection.execute(
                "UPDATE users SET password_hash=?,client_secret=NULL,"
                "client_lookup=NULL WHERE id=?",
                (encoded, row["id"]),
            )
            connection.execute(
                "DELETE FROM sessions WHERE user_id=?", (row["id"],)
            )

    def login(self, username: str, password: str, address: str):
        now = int(time.time())
        scopes = (
            ("user:" + digest(username), 10),
            ("ip:" + digest(address), 60),
        )
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM login_limits WHERE expires<=?", (now,)
            )
            for scope, limit in scopes:
                row = connection.execute(
                    "SELECT attempts FROM login_limits WHERE scope=?", (scope,)
                ).fetchone()
                if row and row["attempts"] >= limit:
                    return None
            for scope, _ in scopes:
                connection.execute(
                    "INSERT INTO login_limits VALUES(?,1,?) "
                    "ON CONFLICT(scope) DO UPDATE SET attempts=attempts+1",
                    (scope, now + 60),
                )
        row = self.user(username)
        encoded = row["password_hash"] if row else self.dummy_password
        if not verify_password(password, encoded) or row is None:
            return None
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.transaction() as connection:
            # Password reset racing verification must not create a new session.
            current = connection.execute(
                "SELECT password_hash FROM users WHERE id=?", (row["id"],)
            ).fetchone()
            if current is None or current[0] != encoded:
                return None
            connection.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            connection.execute(
                "INSERT INTO sessions VALUES(?,?,?,?)",
                (digest(token), row["id"], csrf, now + SESSION_SECONDS),
            )
        return token

    def session(self, token: str):
        with self.connection() as connection:
            return connection.execute(
                "SELECT users.*, sessions.csrf, sessions.token_hash "
                "FROM users JOIN sessions ON users.id=sessions.user_id "
                "WHERE sessions.token_hash=? AND sessions.expires>?",
                (digest(token), int(time.time())),
            ).fetchone()

    def logout(self, token: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash=?", (digest(token),)
            )

    def rotate_client_secret(self, user_id: int) -> str:
        secret = secrets.token_urlsafe(32)
        with self.transaction() as connection:
            connection.execute(
                "UPDATE users SET client_secret=?, client_lookup=? WHERE id=?",
                (self.encrypt(secret), digest(secret), user_id),
            )
        return secret

    def client_user(self, parameters: dict[str, str]):
        if "apiKey" in parameters:
            with self.connection() as connection:
                return connection.execute(
                    "SELECT * FROM users WHERE client_lookup=?",
                    (digest(parameters["apiKey"]),),
                ).fetchone()
        row = self.user(parameters.get("u", ""))
        if row is None or not row["client_secret"]:
            return None
        secret = self.decrypt(row["client_secret"])
        if "p" in parameters:
            supplied = parameters["p"]
            if supplied.startswith("enc:"):
                try:
                    supplied = bytes.fromhex(supplied[4:]).decode()
                except ValueError, UnicodeError:
                    return None
            valid = hmac.compare_digest(supplied.encode(), secret.encode())
        else:
            salt = parameters.get("s", "")
            expected = hashlib.md5(
                (secret + salt).encode(), usedforsecurity=False
            ).hexdigest()
            valid = bool(salt) and hmac.compare_digest(
                parameters.get("t", "").encode(), expected.encode()
            )
        return row if valid else None

    def start_link(self, session_hash: str) -> str:
        state = secrets.token_urlsafe(32)
        now = int(time.time())
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM link_flows WHERE expires<=?", (now,)
            )
            connection.execute(
                "DELETE FROM link_flows WHERE session_hash=?", (session_hash,)
            )
            connection.execute(
                "INSERT INTO link_flows VALUES(?,?,?)",
                (digest(state), session_hash, now + 600),
            )
        return state

    def claim_link(self, state: str, token: str, session_hash: str) -> bool:
        with self.transaction() as connection:
            result = connection.execute(
                "DELETE FROM link_flows WHERE state_hash=? "
                "AND session_hash=? AND expires>?",
                (digest(state), session_hash, int(time.time())),
            )
            if result.rowcount != 1:
                return False
            # Retain a digest tombstone so a provider token cannot be reused
            # with a newly generated local state or a second account.
            result = connection.execute(
                "INSERT OR IGNORE INTO used_link_tokens VALUES(?)",
                (digest(token),),
            )
            return result.rowcount == 1

    def finish_link(self, session_hash: str, name: str, secret: str) -> bool:
        with self.transaction() as connection:
            result = connection.execute(
                "UPDATE users SET lastfm_name=?,lastfm_secret=?,"
                "link_status='linked' WHERE id=(SELECT user_id FROM sessions "
                "WHERE token_hash=? AND expires>?)",
                (name, self.encrypt(secret), session_hash, int(time.time())),
            )
            return result.rowcount == 1

    def mark_relink(self, user_id: int) -> None:
        """Call only after a provider reports an invalid session (error 9)."""
        with self.transaction() as connection:
            connection.execute(
                "UPDATE users SET link_status='relink',lastfm_secret=NULL "
                "WHERE id=?",
                (user_id,),
            )
