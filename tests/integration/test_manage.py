"""Public account-management commands use production storage composition."""

import os
import subprocess
import sys

from support.build_accounts import make_accounts


def test_management_cli_initializes_new_storage_and_resets_password(tmp_path):
    directory = tmp_path / "new" / "data"

    def command(action, password):
        result = subprocess.run(
            [sys.executable, "-m", "lastbridge.manage", action, "admin"],
            input=f"{password}\n{password}\n",
            text=True,
            capture_output=True,
            cwd=tmp_path,
            env=os.environ | {"LASTBRIDGE_DATA_DIR": str(directory)},
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert "Account updated" in result.stdout
        assert password not in result.stdout + result.stderr

    command("bootstrap-admin", "12345678")
    accounts = make_accounts(directory)
    assert accounts.user("admin")["admin"] == 1
    assert accounts.login("admin", "12345678", "synthetic")
    client_secret = accounts.rotate_client_secret(accounts.user("admin")["id"])
    command("reset-password", "abcdefgh")
    assert not accounts.login("admin", "12345678", "synthetic")
    assert accounts.login("admin", "abcdefgh", "synthetic")
    assert accounts.client_user({"apiKey": client_secret}) is None
