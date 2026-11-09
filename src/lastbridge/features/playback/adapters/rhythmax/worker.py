import asyncio
import contextlib
import json
import logging
import math
import subprocess
import sys
import tempfile
from html import unescape
from pathlib import Path

import httpx

from lastbridge.features.playback.adapters.rhythmax.client import (
    RhythmaxClient,
    RhythmaxError,
)
from lastbridge.shared.recordings import normalize, normalize_title


def choose_candidate(rows: list[dict], recording: dict) -> str | None:
    artist = normalize(recording.get("artist", ""))
    title = normalize_title(recording.get("title", ""))
    album = normalize(recording.get("album", ""))
    duration = recording.get("duration")
    candidates = []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "song":
            continue
        track_id = str(row.get("id", ""))
        row_title = row.get("title")
        if not track_id or not isinstance(row_title, str):
            continue
        info = row.get("more_info")
        if not isinstance(info, dict):
            continue
        primary = info.get("artists", {}).get("primary", [])
        if artist not in {
            normalize(unescape(item.get("name", "")))
            for item in primary
            if isinstance(item, dict)
        }:
            continue
        if normalize_title(unescape(row_title)) != title:
            continue
        release_name = normalize(unescape(info.get("album", "")))
        if album and release_name != album:
            continue
        try:
            duration_delta = abs(
                float(info.get("duration", 0)) - float(duration or 0)
            )
        except TypeError, ValueError:
            continue
        if duration and duration_delta > 5:
            continue
        candidates.append((duration_delta, track_id))
    if not candidates:
        return None
    candidates.sort()
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    return candidates[0][1]


async def resolve(api: RhythmaxClient, recording: dict) -> dict:
    query = " ".join(
        str(recording.get(key, ""))
        for key in ("artist", "title")
        if recording.get(key)
    )
    if not query:
        raise RhythmaxError("no_match")
    payload = await api.search(query)
    track_id = choose_candidate(payload, recording)
    if track_id is None:
        raise RhythmaxError("no_match")
    return next(row for row in payload if str(row.get("id")) == track_id)


async def download(payload: dict) -> dict:
    directory = Path(payload["directory"])
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True
    ) as http:
        api = RhythmaxClient(http)
        track = await resolve(api, payload["recording"])
        encrypted = track["more_info"].get("encrypted_media_url")
        if not isinstance(encrypted, str) or not encrypted:
            raise RhythmaxError("no_audio_format")
        source = directory / "rhythmax-source.mp4"
        try:
            await api.fetch_audio(
                encrypted, source, payload["maximum"], payload["reserve"]
            )
            source_probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_name,codec_type,bit_rate:format=duration",
                    "-of",
                    "json",
                    str(source),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            source_info = json.loads(source_probe.stdout)
            audio = [
                s
                for s in source_info.get("streams", [])
                if s.get("codec_type") == "audio"
            ]
            if len(audio) != 1 or audio[0].get("codec_name") != "aac":
                raise RhythmaxError("invalid_audio")
            if float(audio[0].get("bit_rate", 0)) < 300_000:
                raise RhythmaxError("low_quality")
            source_duration = float(
                source_info.get("format", {}).get("duration", 0)
            )
            expected_duration = float(track["more_info"].get("duration", 0))
            if (
                not math.isfinite(source_duration)
                or source_duration <= 0
                or (
                    expected_duration
                    and abs(source_duration - expected_duration) > 5
                )
            ):
                raise RhythmaxError("invalid_audio")
            extension = "m4a" if payload["policy"] == "aac" else "webm"
            destination = directory / f"audio.{extension}"
            if payload["policy"] == "aac":
                codec = ["-c:a", "copy", "-f", "mp4"]
            else:
                codec = [
                    "-c:a",
                    "libopus",
                    "-b:a",
                    "320k",
                    "-f",
                    "webm",
                ]
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-xerror",
                    "-y",
                    "-i",
                    str(source),
                    "-map",
                    "0:a:0",
                    *codec,
                    "-fs",
                    str(payload["maximum"]),
                    str(destination),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=180,
                check=True,
            )
            if (
                not destination.is_file()
                or not 0 < destination.stat().st_size <= payload["maximum"]
            ):
                raise RhythmaxError("too_large")
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type,codec_name:format=duration,size",
                    "-of",
                    "json",
                    str(destination),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            measured = json.loads(probe.stdout)
            expected = "aac" if payload["policy"] == "aac" else "opus"
            duration = float(measured.get("format", {}).get("duration", 0))
            streams = measured.get("streams", [])
            if (
                len(streams) != 1
                or streams[0].get("codec_name") != expected
                or not math.isfinite(duration)
                or duration <= 0
                or abs(duration - source_duration) > 1
            ):
                raise RhythmaxError("invalid_audio")
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-xerror",
                    "-i",
                    str(destination),
                    "-map",
                    "0:a:0",
                    "-f",
                    "null",
                    "-",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=True,
            )
            return {"policy": payload["policy"], "ffprobe": measured}
        finally:
            source.unlink(missing_ok=True)


def main():
    logging.disable(logging.CRITICAL)
    payload = json.load(sys.stdin)
    with tempfile.TemporaryFile(mode="w+") as sink:
        with (
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            try:
                result = asyncio.run(download(payload))
            except asyncio.CancelledError:
                raise
            except RhythmaxError as error:
                result = {"error": error.reason}
                if error.status is not None:
                    result["status"] = error.status
            except Exception:
                result = {"error": "provider_error"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
