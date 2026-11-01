import sqlite3


class AccountRepository:
    def __init__(self, database):
        self.database = database

    def user(self, username):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username=?", (username,)
            ).fetchone()
            return dict(row) if row is not None else None

    def create_user(self, username, encoded, bootstrap):
        with self.database.transaction() as connection:
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

    def reset_password(self, username, encoded):
        with self.database.transaction() as connection:
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

    def allow_login(self, scopes, now):
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM login_limits WHERE expires<=?", (now,)
            )
            for scope, limit in scopes:
                row = connection.execute(
                    "SELECT attempts FROM login_limits WHERE scope=?", (scope,)
                ).fetchone()
                if row and row["attempts"] >= limit:
                    return False
            for scope, _ in scopes:
                connection.execute(
                    "INSERT INTO login_limits VALUES(?,1,?) "
                    "ON CONFLICT(scope) DO UPDATE SET attempts=attempts+1",
                    (scope, now + 60),
                )
        return True

    def create_session(self, user_id, encoded, token_hash, csrf, now, expiry):
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT password_hash FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if current is None or current[0] != encoded:
                return False
            connection.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            connection.execute(
                "INSERT INTO sessions VALUES(?,?,?,?)",
                (token_hash, user_id, csrf, expiry),
            )
        return True

    def session(self, token_hash, now):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT users.*, sessions.csrf, sessions.token_hash "
                "FROM users JOIN sessions ON users.id=sessions.user_id "
                "WHERE sessions.token_hash=? AND sessions.expires>?",
                (token_hash, now),
            ).fetchone()
            return dict(row) if row is not None else None

    def logout(self, token_hash):
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash=?", (token_hash,)
            )

    def rotate_client_secret(self, user_id, encrypted, lookup):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE users SET client_secret=?, client_lookup=? WHERE id=?",
                (encrypted, lookup, user_id),
            )

    def client_lookup(self, lookup):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE client_lookup=?", (lookup,)
            ).fetchone()
            return dict(row) if row is not None else None

    def start_link(self, state_hash, session_hash, now):
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM link_flows WHERE expires<=?", (now,)
            )
            connection.execute(
                "DELETE FROM link_flows WHERE session_hash=?", (session_hash,)
            )
            connection.execute(
                "INSERT INTO link_flows VALUES(?,?,?)",
                (state_hash, session_hash, now + 600),
            )

    def claim_link(self, state_hash, token_hash, session_hash, now):
        with self.database.transaction() as connection:
            result = connection.execute(
                "DELETE FROM link_flows WHERE state_hash=? "
                "AND session_hash=? AND expires>?",
                (state_hash, session_hash, now),
            )
            if result.rowcount != 1:
                return False
            result = connection.execute(
                "INSERT OR IGNORE INTO used_link_tokens VALUES(?)",
                (token_hash,),
            )
            return result.rowcount == 1

    def finish_link(self, session_hash, name, encrypted, now):
        with self.database.transaction() as connection:
            result = connection.execute(
                "UPDATE users SET lastfm_name=?,lastfm_secret=?,"
                "link_status='linked' WHERE id=(SELECT user_id FROM sessions "
                "WHERE token_hash=? AND expires>?)",
                (name, encrypted, session_hash, now),
            )
            return result.rowcount == 1

    def mark_relink(self, user_id):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE users SET link_status='relink',lastfm_secret=NULL "
                "WHERE id=?",
                (user_id,),
            )
