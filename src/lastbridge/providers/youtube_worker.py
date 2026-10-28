import contextlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from lastbridge.providers.base import ProviderError
from lastbridge.providers.youtube import VIDEO_ID


class QuietLogger:
    def debug(self, message: str) -> None:
        pass

    info = warning = error = debug


def classify_error(message: str) -> str:
    message = message.casefold()
    if any(word in message for word in ("sign in", "bot", "403", "429")):
        return "access_denied"
    if any(
        word in message for word in ("unavailable", "private video", "removed")
    ):
        return "unavailable"
    if any(
        word in message
        for word in ("connection", "timed out", "resolve", "network", "ssl")
    ):
        return "network_error"
    return "provider_error"


def search(payload: dict[str, Any]) -> dict[str, Any]:
    from ytmusicapi import YTMusic

    recording = payload["recording"]
    query = " ".join(
        recording[key]
        for key in ("artist", "title", "album")
        if recording[key]
    )
    # No auth, cookies, global yt-dlp config, or browser credential discovery.
    rows = YTMusic().search(query, filter="songs", limit=10)
    candidates = []
    for row in rows[:10]:
        source_id = row.get("videoId", "")
        if not VIDEO_ID.fullmatch(source_id) or not row.get("title"):
            continue
        album = row.get("album") or {}
        candidates.append(
            {
                "source_id": source_id,
                "title": row["title"],
                "artists": [
                    artist["name"]
                    for artist in row.get("artists", [])
                    if artist.get("name")
                ],
                "album": album.get("name"),
                "duration_seconds": row.get("duration_seconds"),
                "catalog_audio": (
                    row.get("resultType") == "song"
                    and row.get("videoType") == "MUSIC_VIDEO_TYPE_ATV"
                    and bool(album.get("name"))
                ),
            }
        )
    return {"candidates": candidates}


def audio_formats(info: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in info.get("formats", [])
        if item.get("vcodec") == "none"
        and item.get("acodec") not in (None, "none")
        and item.get("url", "").startswith("https://")
        and item.get("protocol") == "https"
        and not item.get("has_drm")
    ]


def select_format(formats: list[dict[str, Any]]) -> dict[str, Any]:
    # Best source AAC/M4A first for client compatibility; Opus fallback.
    compatible = [
        item
        for item in formats
        if (item.get("ext") == "m4a" and item["acodec"].startswith("mp4a"))
        or (item.get("ext") == "webm" and item["acodec"] == "opus")
    ]
    if not compatible:
        raise ProviderError("no_audio_format")
    return max(
        compatible,
        key=lambda item: (
            item["ext"] == "m4a",
            item.get("abr") or item.get("tbr") or 0,
        ),
    )


def format_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "format_id",
            "ext",
            "acodec",
            "abr",
            "asr",
            "audio_channels",
            "filesize",
            "filesize_approx",
        )
    }


def sample_audio(
    selected: dict[str, Any], source_id: str, directory: Path
) -> dict[str, Any]:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ProviderError("media_tools_missing")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{source_id}.{selected['ext']}"
    # A timeout can kill this worker before cleanup. Parent owns this directory
    # and removes the entire temporary attempt on every failed run.
    sample = directory / f"sample.{selected['ext']}"
    headers = "".join(
        f"{key}: {value}\r\n"
        for key, value in selected.get("http_headers", {}).items()
        if "\r" not in key + value and "\n" not in key + value
    )
    started = time.monotonic()
    completed = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-rw_timeout",
            "10000000",
            "-headers",
            headers,
            "-i",
            selected["url"],
            "-t",
            "10",
            "-map",
            "0:a:0",
            "-c:a",
            "copy",
            str(sample),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=25,
        check=False,
    )
    if completed.returncode:
        category = classify_error(completed.stderr)
        if category in ("access_denied", "network_error"):
            raise ProviderError(f"sample_{category}")
        raise ProviderError("sample_fetch_failed")
    # Probe and actually decode the result, without transcoding the saved file.
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,bit_rate:"
            "format=duration,size,bit_rate",
            "-of",
            "json",
            str(sample),
        ],
        capture_output=True,
        timeout=5,
        check=True,
        text=True,
    )
    measured = json.loads(probe.stdout)
    if (
        not measured.get("streams")
        or float(measured.get("format", {}).get("duration", 0)) < 8
    ):
        raise ProviderError("sample_failed")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(sample),
            "-f",
            "null",
            "-",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=True,
    )
    sample.rename(destination)
    return {
        "file": destination.name,
        "seconds": round(time.monotonic() - started, 3),
        "ffprobe": measured,
        "decode_ok": True,
    }


def inspect_audio(payload: dict[str, Any]) -> dict[str, Any]:
    from yt_dlp import YoutubeDL

    source_id = payload["source_id"]
    if not VIDEO_ID.fullmatch(source_id):
        raise ProviderError("provider_error")
    started = time.monotonic()
    options = {
        "quiet": True,
        "logger": QuietLogger(),
        "noplaylist": True,
        "cachedir": False,
        "socket_timeout": 10,
        "retries": 0,
        "extractor_retries": 0,
        "js_runtimes": {payload["js_runtime"]: {}},
        "remote_components": [],
    }
    if payload.get("player_client", "default") != "default":
        options["extractor_args"] = {
            "youtube": {"player_client": [payload["player_client"]]}
        }
    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(
            f"https://music.youtube.com/watch?v={source_id}", download=False
        )
    if info.get("is_live"):
        raise ProviderError("unavailable")
    formats = audio_formats(info)
    selected = select_format(formats)
    selected = selected | {
        "http_headers": info.get("http_headers", {})
        | selected.get("http_headers", {})
    }
    result = {
        "resolve_seconds": round(time.monotonic() - started, 3),
        "available_formats": [format_summary(item) for item in formats],
        "selected_format": format_summary(selected),
    }
    if payload.get("sample_dir"):
        try:
            result["sample"] = sample_audio(
                selected, source_id, Path(payload["sample_dir"])
            )
        except ProviderError as error:
            result["sample_error"] = str(error)
        except subprocess.SubprocessError, OSError:
            result["sample_error"] = "sample_failed"
    return result


def main() -> None:
    logging.disable(logging.CRITICAL)
    payload = json.load(sys.stdin)
    # Third-party progress/error output is deliberately not part of our report.
    with tempfile.TemporaryFile(mode="w+") as sink:
        with (
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            try:
                if payload["operation"] == "search":
                    result = search(payload)
                else:
                    result = inspect_audio(payload)
            except ImportError:
                result = {"error": "dependencies_missing"}
            except ProviderError as error:
                result = {"error": str(error)}
            except Exception as error:
                result = {"error": classify_error(str(error))}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
