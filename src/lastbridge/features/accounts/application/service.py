import hashlib
import hmac
import secrets
import time

from lastbridge.features.accounts.application.ports import AccountStore, Cipher
from lastbridge.features.accounts.domain.security import (
    SESSION_SECONDS,
    digest,
    password_hash,
    validate_account,
    verify_password,
)


class Accounts:
    def __init__(self, repository: AccountStore, credentials: Cipher):
        self.repository = repository
        self.credentials = credentials
        self.dummy_password = password_hash(secrets.token_urlsafe(32))

    def encrypt(self, value: str) -> str:
        return self.credentials.encrypt(value)

    def decrypt(self, value: str) -> str:
        return self.credentials.decrypt(value)

    def user(self, username):
        return self.repository.user(username)

    def create_user(self, username, password, *, bootstrap=False):
        validate_account(username, password)
        self.repository.create_user(
            username, password_hash(password), bootstrap
        )

    def reset_password(self, username, password):
        validate_account(username, password)
        self.repository.reset_password(username, password_hash(password))

    def login(self, username, password, address):
        now = int(time.time())
        scopes = (
            ("user:" + digest(username), 10),
            ("ip:" + digest(address), 60),
        )
        if not self.repository.allow_login(scopes, now):
            return None
        row = self.user(username)
        encoded = row["password_hash"] if row else self.dummy_password
        if not verify_password(password, encoded) or row is None:
            return None
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        if not self.repository.create_session(
            row["id"], encoded, digest(token), csrf, now, now + SESSION_SECONDS
        ):
            return None
        return token

    def session(self, token):
        return self.repository.session(digest(token), int(time.time()))

    def logout(self, token):
        self.repository.logout(digest(token))

    def rotate_client_secret(self, user_id):
        secret = secrets.token_urlsafe(32)
        self.repository.rotate_client_secret(
            user_id, self.encrypt(secret), digest(secret)
        )
        return secret

    def client_user(self, parameters: dict[str, str]):
        if "apiKey" in parameters:
            return self.repository.client_lookup(digest(parameters["apiKey"]))
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

    def start_link(self, session_hash):
        state = secrets.token_urlsafe(32)
        self.repository.start_link(
            digest(state), session_hash, int(time.time())
        )
        return state

    def claim_link(self, state, token, session_hash):
        return self.repository.claim_link(
            digest(state), digest(token), session_hash, int(time.time())
        )

    def finish_link(self, session_hash, name, secret):
        return self.repository.finish_link(
            session_hash, name, self.encrypt(secret), int(time.time())
        )

    def mark_relink(self, user_id):
        self.repository.mark_relink(user_id)
