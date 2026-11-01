import hashlib
import hmac
import re
import secrets

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
        raise ValueError("Use 1-64 lowercase letters, digits, dots, - or _.")
    if not 8 <= len(password) <= 256:
        raise ValueError("Browser passwords must contain 8-256 characters.")
