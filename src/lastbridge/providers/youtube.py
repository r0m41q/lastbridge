import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from lastbridge.features.catalog.domain.matching import (
    match_recording,
    normalize,
    normalize_title,
)
from lastbridge.providers.base import Candidate, ProviderError, Recording

__all__ = [
    "YouTubeProvider",
    "VIDEO_ID",
    "ERROR_CODES",
    "run_worker",
    "match_recording",
    "normalize",
    "normalize_title",
]

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
ERROR_CODES = frozenset(
    {
        "dependencies_missing",
        "network_error",
        "access_denied",
        "unavailable",
        "no_audio_format",
        "provider_error",
        "media_tools_missing",
        "sample_failed",
    }
)


def run_worker(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Kill the whole process group, including a downloader, on deadline."""
    with subprocess.Popen(
        [sys.executable, "-m", "lastbridge.providers.youtube_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, _ = process.communicate(
                json.dumps(payload), timeout=timeout
            )
        except BaseException as error:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            if isinstance(error, subprocess.TimeoutExpired):
                raise ProviderError("timeout") from None
            raise
    try:
        result = json.loads(stdout)
        if not isinstance(result, dict) or process.returncode != 0:
            raise ValueError
    except ValueError, TypeError:
        raise ProviderError("worker_failed") from None
    if code := result.get("error"):
        raise ProviderError(code if code in ERROR_CODES else "provider_error")
    return result


class YouTubeProvider:
    def __init__(
        self,
        timeout: float = 45,
        js_runtime: str = "node",
        player_client: str = "visionos",
    ) -> None:
        self.timeout = timeout
        self.js_runtime = js_runtime
        self.player_client = player_client

    def search(self, recording: Recording) -> list[Candidate]:
        result = run_worker(
            {"operation": "search", "recording": asdict(recording)},
            self.timeout,
        )
        return [
            Candidate(**(row | {"artists": tuple(row["artists"])}))
            for row in result["candidates"]
        ]

    def inspect(
        self, source_id: str, sample_dir: Path | None = None
    ) -> dict[str, Any]:
        if not VIDEO_ID.fullmatch(source_id):
            raise ProviderError("invalid_source_id")
        payload = {
            "operation": "inspect",
            "source_id": source_id,
            "sample_dir": None,
            "js_runtime": self.js_runtime,
            "player_client": self.player_client,
        }
        if sample_dir is None:
            return run_worker(payload, self.timeout)
        sample_dir.mkdir(parents=True, exist_ok=True)
        # The parent survives worker timeouts and owns partial-file cleanup.
        with tempfile.TemporaryDirectory(dir=sample_dir) as attempt:
            payload["sample_dir"] = attempt
            result = run_worker(payload, self.timeout)
            if result.get("sample_error"):
                return result
            filename = result["sample"]["file"]
            if Path(filename).name != filename:
                raise ProviderError("worker_failed")
            try:
                os.link(Path(attempt) / filename, sample_dir / filename)
            except FileExistsError:
                raise ProviderError("sample_exists") from None
            return result
