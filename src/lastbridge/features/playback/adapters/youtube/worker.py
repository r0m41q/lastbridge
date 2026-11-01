import contextlib
import json
import logging
import math
import resource
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from lastbridge.providers.base import ProviderError
from lastbridge.providers.youtube import VIDEO_ID
from lastbridge.providers.youtube_worker import (
    QuietLogger,
    audio_formats,
    classify_error,
)


def choose_format(formats, policy):
    if policy == "aac":
        choices = [
            f
            for f in formats
            if f.get("ext") == "m4a" and f["acodec"].startswith("mp4a")
        ]
    else:
        choices = [
            f
            for f in formats
            if f.get("ext") == "webm" and f["acodec"] == "opus"
        ]
    if not choices:
        raise ProviderError("no_audio_format")
    return max(choices, key=lambda f: f.get("abr") or f.get("tbr") or 0)


def download(payload):
    from yt_dlp import YoutubeDL

    source, policy = payload["source"], payload["policy"]
    if not VIDEO_ID.fullmatch(source) or policy not in ("aac", "opus"):
        raise ProviderError("unavailable")
    directory = Path(payload["directory"])
    maximum, reserve = payload["maximum"], payload["reserve"]

    def progress(status):
        if status.get("downloaded_bytes", 0) > maximum:
            raise ProviderError("too_large")
        if shutil.disk_usage(directory).free < reserve:
            raise ProviderError("storage_full")

    def select(context):
        if context.get("is_live"):
            raise ProviderError("unavailable")
        selected = choose_format(audio_formats(context), policy)
        if (selected.get("filesize") or 0) > maximum:
            raise ProviderError("too_large")
        yield selected

    # No cookies, account files, global config, or remote scripts.
    # Each attempt resolves fresh URLs and writes only to private staging.
    with YoutubeDL(
        {
            "quiet": True,
            "logger": QuietLogger(),
            "noplaylist": True,
            "cachedir": False,
            "socket_timeout": 10,
            "retries": 1,
            "extractor_retries": 0,
            "continuedl": False,
            "js_runtimes": {},
            "remote_components": [],
            "extractor_args": {"youtube": {"player_client": ["visionos"]}},
            "format": select,
            "outtmpl": str(directory / "audio.%(ext)s"),
            "max_filesize": maximum,
            "progress_hooks": [progress],
            "buffersize": 64 * 1024,
        }
    ) as downloader:
        info = downloader.extract_info(
            f"https://music.youtube.com/watch?v={source}", download=True
        )
    if not info or info.get("is_live"):
        raise ProviderError("unavailable")
    extension = "m4a" if policy == "aac" else "webm"
    path = directory / f"audio.{extension}"
    if not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise ProviderError("too_large")
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,sample_rate,channels:"
            "format=duration,size,bit_rate",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        timeout=10,
        check=True,
        text=True,
    )
    measured = json.loads(probe.stdout)
    streams = measured.get("streams", [])
    expected = "aac" if policy == "aac" else "opus"
    duration = float(measured.get("format", {}).get("duration", 0))
    if (
        len(streams) != 1
        or streams[0].get("codec_name") != expected
        or streams[0].get("codec_type") != "audio"
        or not math.isfinite(duration)
        or duration <= 0
        or (info.get("duration") and duration < info["duration"] - 3)
    ):
        raise ProviderError("unavailable")
    # Decode the complete acquired recording. A valid container header alone
    # does not prove its packets are complete and readable.
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=True,
    )
    return {"policy": policy, "ffprobe": measured}


def main():
    logging.disable(logging.CRITICAL)
    payload = json.load(sys.stdin)
    # Enforce the reservation even when length is unknown or a server sends
    # more data than advertised. This limit belongs only to the child process.
    resource.setrlimit(
        resource.RLIMIT_FSIZE, (payload["maximum"], payload["maximum"])
    )
    with tempfile.TemporaryFile(mode="w+") as sink:
        with (
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            try:
                result = download(payload)
            except ProviderError as error:
                result = {"error": str(error)}
            except Exception as error:
                result = {"error": classify_error(str(error))}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
