import os
from pathlib import Path

from cryptography.fernet import Fernet


class Credentials:
    def __init__(self, directory: Path, database_path: Path):
        key_path = directory / "credentials.key"
        # A missing key on an existing database is a recovery problem, not a
        # reason to silently replace the key and lose encrypted credentials.
        if not key_path.exists():
            if database_path.exists():
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

    def encrypt(self, value: str) -> str:
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self.cipher.decrypt(value.encode()).decode()
