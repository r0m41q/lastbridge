import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from lastbridge.probe import probe_one
from lastbridge.providers.base import Candidate, ProviderError, Recording
from lastbridge.providers.youtube import (
    YouTubeProvider,
    match_recording,
    run_worker,
)
from lastbridge.providers.youtube_worker import (
    audio_formats,
    classify_error,
    format_summary,
    select_format,
)

RECORDING = Recording("Björk", "Jóga", "Homogenic", 305)
CANDIDATE = Candidate(
    "abcdefghijk", "Jóga", ("Björk",), "Homogenic", 305, True
)


def test_match_handles_unicode_and_duplicate_ids() -> None:
    decomposed = replace(CANDIDATE, title="Jo\u0301ga")
    assert match_recording(RECORDING, [CANDIDATE, decomposed]) == decomposed


def test_match_allows_performer_credit_but_keeps_version_qualifiers() -> None:
    credit = replace(CANDIDATE, title="Jóga (feat. Guest)")
    assert match_recording(RECORDING, [credit]) == credit
    live_credit = replace(credit, title="Jóga (Live) (feat. Guest)")
    with pytest.raises(ProviderError, match="^no_match$"):
        match_recording(RECORDING, [live_credit])


@pytest.mark.parametrize(
    "candidate",
    [
        replace(CANDIDATE, title="Jóga (Live)"),
        replace(CANDIDATE, title="Jóga (Remix)"),
        replace(CANDIDATE, title="Jóga (Instrumental)"),
        replace(CANDIDATE, artists=("Cover Artist",)),
        replace(CANDIDATE, album="Live in London"),
        replace(CANDIDATE, duration_seconds=320),
        replace(CANDIDATE, duration_seconds=None),
        replace(CANDIDATE, catalog_audio=False),
    ],
)
def test_match_refuses_wrong_or_unverified_recording(
    candidate: Candidate,
) -> None:
    with pytest.raises(ProviderError, match="^no_match$"):
        match_recording(RECORDING, [candidate])


def test_match_refuses_ambiguous_provider_ids() -> None:
    with pytest.raises(ProviderError, match="^ambiguous_match$"):
        match_recording(
            RECORDING, [CANDIDATE, replace(CANDIDATE, source_id="12345678901")]
        )


def test_audio_policy_and_report_do_not_expose_signed_urls() -> None:
    common = {
        "url": "https://example.invalid/audio?token=secret",
        "protocol": "https",
        "vcodec": "none",
    }
    aac_low = common | {
        "format_id": "139",
        "acodec": "mp4a.40.5",
        "ext": "m4a",
        "abr": 48,
    }
    aac_high = aac_low | {"format_id": "140", "abr": 129}
    opus = common | {"acodec": "opus", "ext": "webm", "abr": 160}
    formats = audio_formats(
        {
            "formats": [
                aac_low,
                aac_high,
                opus,
                aac_high | {"has_drm": True, "abr": 500},
                aac_high | {"vcodec": "avc1", "abr": 600},
                aac_high | {"protocol": "m3u8_native", "abr": 700},
            ]
        }
    )
    assert len(formats) == 3
    assert select_format(formats) == aac_high
    assert select_format([opus]) == opus
    assert "secret" not in json.dumps(format_summary(aac_high))
    with pytest.raises(ProviderError, match="^no_audio_format$"):
        select_format([])


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Sign in to confirm you're not a bot token=secret", "access_denied"),
        ("HTTPSConnectionPool: name resolution failed", "network_error"),
        ("Video unavailable", "unavailable"),
        ("Unexpected token=secret", "provider_error"),
    ],
)
def test_provider_errors_are_fixed_codes(message: str, expected: str) -> None:
    assert classify_error(message) == expected


def test_worker_deadline_kills_child_processes(
    monkeypatch, tmp_path: Path
) -> None:
    child_pid_file = tmp_path / "child.pid"
    helper = tmp_path / "slow_worker.py"
    helper.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'])\n"
        f"Path({str(child_pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(30)\n"
    )
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda command, **kwargs: real_popen(
            [sys.executable, str(helper)], **kwargs
        ),
    )
    started = time.monotonic()
    with pytest.raises(ProviderError, match="^timeout$"):
        run_worker({}, timeout=1)
    assert time.monotonic() - started < 4
    child_pid = int(child_pid_file.read_text())
    proc_stat = Path(f"/proc/{child_pid}/stat")
    # A killed orphan can remain as a zombie until the host reaps it.
    deadline = time.monotonic() + 2
    while proc_stat.exists() and proc_stat.read_text().split()[2] != "Z":
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_timed_out_sample_removes_partial_files(
    monkeypatch, tmp_path: Path
) -> None:
    def failing_worker(payload, timeout):
        (Path(payload["sample_dir"]) / "partial.m4a").write_bytes(b"partial")
        raise ProviderError("timeout")

    monkeypatch.setattr(
        "lastbridge.providers.youtube.run_worker", failing_worker
    )
    with pytest.raises(ProviderError, match="^timeout$"):
        YouTubeProvider().inspect(CANDIDATE.source_id, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_sample_does_not_overwrite_existing_file(
    monkeypatch, tmp_path: Path
) -> None:
    filename = f"{CANDIDATE.source_id}.m4a"
    original = tmp_path / filename
    original.write_bytes(b"keep this")

    def successful_worker(payload, timeout):
        (Path(payload["sample_dir"]) / filename).write_bytes(b"new")
        return {"sample": {"file": filename}}

    monkeypatch.setattr(
        "lastbridge.providers.youtube.run_worker", successful_worker
    )
    with pytest.raises(ProviderError, match="^sample_exists$"):
        YouTubeProvider().inspect(CANDIDATE.source_id, tmp_path)
    assert original.read_bytes() == b"keep this"
    assert list(tmp_path.iterdir()) == [original]


def test_probe_does_not_claim_playback_after_resolution_only() -> None:
    class Provider:
        def inspect(self, source_id, sample_dir):
            return {"selected_format": {"acodec": "mp4a.40.2"}}

    result = probe_one(Provider(), None, CANDIDATE.source_id, None)
    assert result["status"] == "resolved_only"
    assert result["matching"] == "bypassed_for_explicit_id"


def test_cli_requires_opt_in_before_any_network_call() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "lastbridge.probe", "--suite"],
        capture_output=True,
        text=True,
        timeout=3,
        env=os.environ | {"HTTPS_PROXY": "http://127.0.0.1:1"},
    )
    assert completed.returncode == 2
    assert "require explicit --live" in completed.stderr
