import asyncio
import io
import json
import sys

import pytest

from lastbridge.features.playback.adapters.youtube import worker
from lastbridge.features.playback.adapters.youtube.diagnostics import (
    DiagnosticLogger,
    failure_reason,
)
from lastbridge.features.playback.adapters.youtube.provider import (
    AudioProvider,
)
from lastbridge.shared.errors import CatalogError


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("ERROR: Postprocessing: Option not found", "ffmpeg_option_missing"),
        (
            "Requested output format 'ipod' is not known",
            "ffmpeg_muxer_missing",
        ),
        ("HTTP Error 403: Forbidden", "access_denied"),
        ("Sign in to confirm you're not a bot", "access_denied"),
        ("Requested format is not available", "no_audio_format"),
        ("Connection timed out", "network_error"),
    ],
)
def test_worker_failure_categories(message, reason):
    assert failure_reason(message) == reason


def test_worker_reports_stage_and_drops_upstream_secrets(monkeypatch, capsys):
    def fail(payload, diagnostics):
        diagnostics.update(stage="postprocess", yt_dlp_version="2026.10.26")
        logger = DiagnosticLogger(diagnostics)
        logger.warning("HTTP Error 403: https://secret.invalid/?token=secret")
        raise RuntimeError("Postprocessing: Option not found secret")

    monkeypatch.setattr(worker, "download", fail)
    monkeypatch.setattr(worker.logging, "disable", lambda *args: None)
    monkeypatch.setattr(worker.resource, "setrlimit", lambda *args: None)
    monkeypatch.setattr(
        worker.sys,
        "stdin",
        io.StringIO(json.dumps({"maximum": 8192})),
    )
    worker.main()
    output = capsys.readouterr().out
    assert "secret" not in output
    report = json.loads(output)
    assert report["error"] == "ffmpeg_option_missing"
    assert report["diagnostics"]["stage"] == "postprocess"
    assert report["diagnostics"]["http_status"] == 403
    assert report["diagnostics"]["exception_type"] == "RuntimeError"


def test_parent_logs_useful_diagnostics_without_untrusted_values(
    tmp_path, monkeypatch, caplog
):
    async def scenario():
        original = asyncio.create_subprocess_exec
        report = {
            "error": "https://secret.invalid/token",
            "diagnostics": {
                "stage": "postprocess",
                "yt_dlp_version": "2026.10.26",
                "http_status": 403,
                "exception_type": "DownloadError",
                "format_id": "https://secret.invalid/token",
                "arbitrary": "secret",
            },
        }
        script = (
            "import sys; sys.stdin.read(); print("
            + repr(json.dumps(report))
            + ")"
        )

        async def launch(*args, **kwargs):
            return await original(sys.executable, "-c", script, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
        with pytest.raises(CatalogError):
            await AudioProvider().fetch(
                "GpP4pLtX4BA", "aac", tmp_path, 8192, 0
            )

    asyncio.run(scenario())
    assert "postprocess" in caplog.text
    assert "DownloadError" in caplog.text
    assert "2026.10.26" in caplog.text
    assert "403" in caplog.text
    assert "secret" not in caplog.text
