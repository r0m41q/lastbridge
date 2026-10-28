import argparse
import getpass
import sqlite3

from cryptography.fernet import InvalidToken
from pydantic import ValidationError

from lastbridge.accounts import Accounts
from lastbridge.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("bootstrap-admin", "reset-password")
    )
    parser.add_argument("username")
    args = parser.parse_args()
    password = getpass.getpass("New browser password: ")
    if password != getpass.getpass("Repeat browser password: "):
        parser.exit(2, "Passwords do not match.\n")
    try:
        store = Accounts(Settings().data_dir)
        if args.action == "bootstrap-admin":
            store.create_user(args.username, password, bootstrap=True)
        else:
            store.reset_password(args.username, password)
    except OSError, RuntimeError, ValidationError, InvalidToken, sqlite3.Error:
        parser.exit(2, "Account setup failed; check configuration/storage.\n")
    except ValueError as error:
        parser.exit(2, str(error) + "\n")
    print("Account updated. Sign in through the browser.")


if __name__ == "__main__":
    main()
