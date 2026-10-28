import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import pytest


def test_cli_startup_requests_and_shutdown(tmp_path: Path) -> None:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    environment = os.environ | {
        "LASTBRIDGE_HOST": "127.0.0.1",
        "LASTBRIDGE_PORT": str(port),
        "LASTBRIDGE_DATA_DIR": str(tmp_path / "data"),
        "LASTBRIDGE_LOG_LEVEL": "DEBUG",
    }
    base_url = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))
    log_path = tmp_path / "server.log"
    with log_path.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "lastbridge"],
            cwd=tmp_path,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, log_path.read_text()
                try:
                    with opener.open(
                        f"{base_url}/health/ready", timeout=1
                    ) as response:
                        assert response.status == 200
                        break
                except URLError:
                    assert time.monotonic() < deadline, log_path.read_text()
                    time.sleep(0.05)

            request = Request(
                f"{base_url}/rest/ping.view?p=cli-password&t=cli-token",
                headers={"Authorization": "Bearer cli-bearer"},
            )
            with pytest.raises(HTTPError) as error:
                opener.open(request, timeout=2)
            assert error.value.code == 404
            error.value.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                pytest.fail("Server did not shut down gracefully")

    # Uvicorn restores SIGTERM after completing lifespan shutdown.
    assert process.returncode in (0, -signal.SIGTERM)
    logs = log_path.read_text()
    assert "Lastbridge ready" in logs
    assert "Lastbridge stopped" in logs
    for secret in ("cli-password", "cli-token", "cli-bearer"):
        assert secret not in logs
