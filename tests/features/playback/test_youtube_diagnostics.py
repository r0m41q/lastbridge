import asyncio
import io
import json
import sys

import pytest
from support.audio import youtube_http_error
from yt_dlp.utils import DownloadError

from lastbridge.features.playback.adapters.youtube import worker
from lastbridge.features.playback.adapters.youtube.diagnostics import (
    DiagnosticLogger,
    failure_detail,
    failure_reason,
    safe_diagnostics,
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


@pytest.mark.parametrize(
    ("message", "detail"),
    [
        ("HTTP Error 403: Forbidden", "http_403_unspecified"),
        (
            "HTTP Error 403: https://secret.invalid/?signature=secret",
            "http_403_unspecified",
        ),
        ("Sign in to confirm you're not a bot", "bot_confirmation_required"),
        ("HTTP Error 429: Too Many Requests", "rate_limited"),
        ("You have exceeded the rate limit", "rate_limited"),
        ("This content isn't available, try again later", "rate_limited"),
        ("A PO Token is required for this client", "po_token_required"),
        ("Missing po_token", "po_token_required"),
        ("Sign in to confirm your age", "age_restricted"),
        ("This video is age-restricted", "age_restricted"),
        ("This video is not available in your country", "geo_restricted"),
        ("Sign in to watch this video", "login_required"),
        ("Private video", "private_recording"),
        ("This video has been removed", "recording_removed"),
        ("Signature challenge failed", "player_challenge_failed"),
        ("Connection timed out", None),
        ("No explanation provided", None),
    ],
)
def test_explicit_failure_details(message, detail):
    assert failure_detail(message + " https://secret.invalid/token") == detail


def test_skipped_format_warning_is_not_proof_of_failure_cause():
    diagnostics = {"stage": "select_format"}
    logger = DiagnosticLogger(diagnostics)
    logger.warning("Skipping formats: missing PO Token")
    logger.failure(
        DownloadError("unable to download video data: HTTP Error 403")
    )
    assert diagnostics["stage"] == "download"
    assert diagnostics["failure_detail"] == "http_403_unspecified"


def test_wrapped_http_failure_preserves_safe_evidence_without_reading_body():
    upstream = youtube_http_error(status=429)
    wrapped = DownloadError(
        "unable to download video data: HTTP Error 429",
        exc_info=(type(upstream), upstream, None),
    )
    outer = RuntimeError("Audio acquisition failed")
    outer.__cause__ = wrapped
    # Exception links may repeat or form cycles.
    wrapped.__context__ = outer
    diagnostics = {"stage": "select_format"}
    DiagnosticLogger(diagnostics).failure(outer)
    assert diagnostics["stage"] == "download"
    assert diagnostics["failure_detail"] == "rate_limited"
    assert diagnostics["http_status"] == 429
    assert diagnostics["request_target"] == "youtube_media"
    assert diagnostics["retry_after_seconds"] == 120
    assert upstream.response.fp.tell() == 0
    assert "secret" not in json.dumps(diagnostics)


def test_explicit_bot_reason_survives_nested_unspecified_http_denial():
    upstream = youtube_http_error()
    wrapped = DownloadError(
        "Sign in to confirm you're not a bot",
        exc_info=(type(upstream), upstream, None),
    )
    diagnostics = {}
    DiagnosticLogger(diagnostics).failure(wrapped)
    assert diagnostics["failure_detail"] == "bot_confirmation_required"
    assert diagnostics["http_status"] == 403


def test_exception_inspection_has_a_depth_bound():
    upstream = youtube_http_error()
    for _ in range(9):
        wrapped = RuntimeError("Acquisition failed")
        wrapped.__cause__ = upstream
        upstream = wrapped
    diagnostics = {}
    DiagnosticLogger(diagnostics).failure(upstream)
    assert diagnostics == {}


@pytest.mark.parametrize(
    ("url", "target", "stage"),
    [
        ("https://www.youtube.com/watch?secret", "youtube", "extract"),
        (
            "https://rr1.googlevideo.com/media?secret",
            "youtube_media",
            "download",
        ),
        ("https://googlevideo.com.evil.invalid/?secret", "other", "extract"),
        ("https://[invalid/?secret", "other", "extract"),
    ],
)
def test_request_target_is_bounded_and_does_not_expose_url(url, target, stage):
    upstream = youtube_http_error(url=url)
    diagnostics = {"stage": "extract"}
    DiagnosticLogger(diagnostics).failure(upstream)
    assert diagnostics["request_target"] == target
    assert diagnostics["stage"] == stage
    assert "secret" not in json.dumps(diagnostics)


@pytest.mark.parametrize("retry", ["secret", "-1", "9999999", "Wed, 11 Nov"])
def test_unusable_retry_header_is_omitted(retry):
    diagnostics = {}
    DiagnosticLogger(diagnostics).failure(youtube_http_error(retry=retry))
    assert "retry_after_seconds" not in diagnostics


def test_parent_rejects_untrusted_detail_target_and_retry_values():
    assert (
        safe_diagnostics(
            {
                "failure_detail": "secret",
                "request_target": "secret",
                "retry_after_seconds": True,
            }
        )
        == {}
    )


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


def test_worker_reports_media_denial_before_first_progress(
    monkeypatch, capsys
):
    def fail(payload, diagnostics):
        diagnostics.update(stage="select_format", format_id="140")
        upstream = youtube_http_error()
        raise DownloadError(
            "unable to download video data: HTTP Error 403: Forbidden",
            exc_info=(type(upstream), upstream, None),
        )

    monkeypatch.setattr(worker, "download", fail)
    monkeypatch.setattr(worker.logging, "disable", lambda *args: None)
    monkeypatch.setattr(worker.resource, "setrlimit", lambda *args: None)
    monkeypatch.setattr(
        worker.sys, "stdin", io.StringIO(json.dumps({"maximum": 8192}))
    )
    worker.main()
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["error"] == "access_denied"
    assert report["diagnostics"]["failure_detail"] == "http_403_unspecified"
    assert report["diagnostics"]["stage"] == "download"
    assert report["diagnostics"]["request_target"] == "youtube_media"
    assert report["diagnostics"]["retry_after_seconds"] == 120
    assert "secret" not in output


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
                "failure_detail": "http_403_unspecified",
                "request_target": "youtube_media",
                "retry_after_seconds": 120,
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
    assert "http_403_unspecified" in caplog.text
    assert "youtube_media" in caplog.text
    assert "retry_after_seconds" in caplog.text
    assert "secret" not in caplog.text
