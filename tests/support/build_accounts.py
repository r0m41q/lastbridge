"""Account setup uses the same storage composition as the application CLI."""

from pathlib import Path

from lastbridge.bootstrap import account_storage
from lastbridge.config import Settings
from lastbridge.features.accounts.application.service import Accounts


def make_accounts(directory: Path) -> Accounts:
    return account_storage(Settings(_env_file=None, data_dir=directory))[2]
