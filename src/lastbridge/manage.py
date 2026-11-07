import argparse
import getpass
import sys

from cryptography.fernet import InvalidToken
from pydantic import ValidationError

from lastbridge.bootstrap import account_storage
from lastbridge.config import Settings
from lastbridge.shared.errors import StorageError


def password(prompt: str) -> str:
    """Read hidden input interactively and piped input in tests/CI."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline().rstrip("\r\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("bootstrap-admin", "reset-password")
    )
    parser.add_argument("username")
    args = parser.parse_args()
    value = password("New browser password: ")
    if value != password("Repeat browser password: "):
        parser.exit(2, "Passwords do not match.\n")
    try:
        _, _, store = account_storage(Settings())
        if args.action == "bootstrap-admin":
            store.create_user(args.username, value, bootstrap=True)
        else:
            store.reset_password(args.username, value)
    except OSError, RuntimeError, ValidationError, InvalidToken, StorageError:
        parser.exit(2, "Account setup failed; check configuration/storage.\n")
    except ValueError as error:
        parser.exit(2, str(error) + "\n")
    print("Account updated. Sign in through the browser.")


if __name__ == "__main__":
    main()
