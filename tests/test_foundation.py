import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from lastbridge.app import create_app
from lastbridge.config import Settings


def test_health_and_lifecycle(tmp_path: Path) -> None:
    app = create_app(Settings(_env_file=None, data_dir=tmp_path / "data"))
    assert not (tmp_path / "data").exists()
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        assert client.get("/health/ready").status_code == 200
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert (tmp_path / "data" / "cache").is_dir()
        assert (tmp_path / "data" / "saved").is_dir()
        assert not app.state.http_client.is_closed
    assert app.state.http_client.is_closed
    assert not app.state.ready


def test_readiness_before_startup(tmp_path: Path) -> None:
    unstarted = TestClient(
        create_app(Settings(_env_file=None, data_dir=tmp_path))
    )
    try:
        assert unstarted.get("/health/live").status_code == 200
        assert unstarted.get("/health/ready").status_code == 503
    finally:
        unstarted.close()


def test_storage_failure_prevents_readiness(tmp_path: Path) -> None:
    data_path = tmp_path / "not-a-directory"
    data_path.write_text("occupied")
    app = create_app(Settings(_env_file=None, data_dir=data_path))
    with pytest.raises(
        RuntimeError, match="Cannot initialize writable Lastbridge storage"
    ):
        with TestClient(app):
            pass
    assert not app.state.ready


def test_environment_overrides_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("LASTBRIDGE_PORT=6000\nLASTBRIDGE_LOG_LEVEL=WARNING\n")
    monkeypatch.setenv("LASTBRIDGE_PORT", "7000")
    monkeypatch.delenv("LASTBRIDGE_LOG_LEVEL", raising=False)
    settings = Settings(_env_file=dotenv)
    assert settings.port == 7000
    assert settings.log_level == "WARNING"


@pytest.mark.parametrize("port", [0, 65536])
def test_invalid_port_rejected(port: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, port=port)


def test_invalid_config_does_not_log_input(tmp_path: Path) -> None:
    environment = os.environ | {
        "LASTBRIDGE_PORT": "credential-like-test-value"
    }
    result = subprocess.run(
        [sys.executable, "-m", "lastbridge"],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "Invalid configuration" in result.stderr
    assert "credential-like-test-value" not in result.stderr + result.stdout


def test_requests_do_not_log_credentials(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="lastbridge")
    with TestClient(
        create_app(Settings(_env_file=None, data_dir=tmp_path))
    ) as client:
        response = client.get(
            "/rest/ping.view?u=alice&p=test-password&t=test-token",
            headers={"Authorization": "Bearer test-bearer"},
        )
        assert response.status_code == 200
        assert 'code="43"' in response.text
    application_messages = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("lastbridge")
    )
    for secret in ("test-password", "test-token", "test-bearer"):
        assert secret not in application_messages
