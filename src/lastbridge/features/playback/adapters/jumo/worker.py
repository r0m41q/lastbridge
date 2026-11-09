import asyncio
import contextlib
import json
import logging
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx

from lastbridge.features.playback.adapters.jumo.client import JumoClient
from lastbridge.features.playback.adapters.jumo.container import (
    parse_init,
    write_segment,
)
from lastbridge.shared.recordings import normalize, normalize_title


def candidate_artist(row: dict) -> str:
    for key in ("performer", "artist"):
        value = row.get(key)
        if isinstance(value, dict) and isinstance(value.get("name"), str):
            return value["name"]
    artists = row.get("artists")
    if isinstance(artists, list) and artists:
        first = artists[0]
        if isinstance(first, dict) and isinstance(first.get("name"), str):
            return first["name"]
    album = row.get("album")
    if isinstance(album, dict):
        value = album.get("artist")
        if isinstance(value, dict) and isinstance(value.get("name"), str):
            return value["name"]
    return ""


def choose_candidate(
    rows: list[dict], recording: dict
) -> tuple[str, str] | None:
    artist = normalize(recording.get("artist", ""))
    title = normalize_title(recording.get("title", ""))
    album = normalize(recording.get("album", ""))
    duration = recording.get("duration")
    candidates = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        track_id = str(row.get("id", ""))
        release = row.get("album")
        album_id = (
            str(release.get("id", "")) if isinstance(release, dict) else ""
        )
        row_title = row.get("title")
        if not track_id or not album_id or not isinstance(row_title, str):
            continue
        if normalize(candidate_artist(row)) != artist:
            continue
        if normalize_title(row_title) != title:
            continue
        release_name = (
            normalize(release.get("title", release.get("name", "")))
            if isinstance(release, dict)
            else ""
        )
        duration_delta = abs(
            float(row.get("duration", 0)) - float(duration or 0)
        )
        if album and release_name != album:
            continue
        if duration and duration_delta > 5:
            continue
        candidates.append((duration_delta, album_id, track_id))
    selected = min(candidates, default=None)
    return (selected[1], selected[2]) if selected is not None else None


async def regions(api: JumoClient) -> tuple[str, set[str]]:
    response = await api.get(JumoClient.REGIONS_PATH)
    if response.status != 200:
        return "CA", set()
    document = json.loads(response.body)
    available = {
        str(row.get("code"))
        for row in document.get("regions", [])
        if isinstance(row, dict) and row.get("available", True)
    }
    default = str(document.get("default", ""))
    return (
        default if default in available else next(iter(available), "CA"),
        available,
    )


async def resolve(
    api: JumoClient, recording: dict, region: str
) -> tuple[str, str]:
    query = " ".join(
        str(recording.get(key, ""))
        for key in ("artist", "title", "album")
        if recording.get(key)
    )
    url = (
        JumoClient.SEARCH_PATH
        + "?query="
        + quote(query)
        + "&offset=0&limit=10&region="
        + quote(region)
        + "&view=ui"
    )
    response = await api.get(url)
    if response.status != 200:
        raise RuntimeError("search_unavailable")
    rows = json.loads(response.body).get("tracks", {}).get("items", [])
    selected = choose_candidate(rows, recording)
    if selected is None:
        raise RuntimeError("no_match")
    return selected


async def descriptor_url(
    api: JumoClient, album_id: str, track_id: str, region: str, quality: str
) -> str:
    format_id = "6" if quality == "cd" else "27"
    url = (
        JumoClient.ALBUM_PATH
        + "?album_id="
        + quote(album_id)
        + "&region="
        + quote(region)
        + "&track_offset=0&track_limit=50&get_track_urls=true&format_id="
        + format_id
    )
    response = await api.get(url)
    if response.status != 200:
        raise RuntimeError("descriptor_unavailable")
    tracks = json.loads(response.body).get("tracks", {})
    for row in tracks.get("items", []):
        if str(row.get("id")) != track_id:
            continue
        fetch_urls = row.get("fetchUrls", {})
        value = fetch_urls.get(format_id)
        if isinstance(value, str):
            return value
    raise RuntimeError("no_audio_format")


async def download(payload: dict) -> dict:
    directory = Path(payload["directory"])
    base_url = payload["base_url"]
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True
    ) as http:
        api = JumoClient(http, base_url)
        default_region, available = await regions(api)
        requested_region = payload.get("region", "").upper()
        region = (
            requested_region
            if requested_region in available
            else default_region
        )
        album_id, track_id = await resolve(api, payload["recording"], region)
        descriptor = await api.get(
            await descriptor_url(
                api, album_id, track_id, region, payload["quality"]
            )
        )
        if descriptor.status != 200:
            raise RuntimeError("descriptor_unavailable")
        qb = json.loads(descriptor.body).get("qb", {})
        if qb.get("preview"):
            raise RuntimeError("preview_only")
        content_key = bytes.fromhex(str(qb["content_key"]))
        segment_count = int(qb["n_segments"])
        template = str(qb["url_template"])
        init_response = await http.get(
            api.url(template.replace("$SEGMENT$", "0"))
        )
        init_response.raise_for_status()
        init = parse_init(init_response.content)
        if len(init.table) != segment_count:
            raise RuntimeError("invalid_segment_table")
        source = directory / "jumo-source.flac"
        with source.open("wb") as output:
            if init.kind != "flac":
                raise RuntimeError("unsupported_container")
            output.write(init.header)
            for index in range(segment_count):
                if shutil.disk_usage(directory).free < payload["reserve"]:
                    raise RuntimeError("storage_full")
                response = await http.get(
                    api.url(template.replace("$SEGMENT$", str(index + 1)))
                )
                response.raise_for_status()
                write_segment(
                    init, index, response.content, content_key, output
                )
        extension = "m4a" if payload["policy"] == "aac" else "webm"
        destination = directory / f"audio.{extension}"
        codec = ["-c:a", "aac", "-b:a", "320k", "-f", "ipod"]
        if payload["policy"] == "opus":
            codec = ["-c:a", "libopus", "-b:a", "320k", "-f", "webm"]
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
            timeout=120,
            check=True,
        )
        source.unlink(missing_ok=True)
        if (
            not destination.is_file()
            or not 0 < destination.stat().st_size <= payload["maximum"]
        ):
            raise RuntimeError("too_large")
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
        ):
            raise RuntimeError("invalid_audio")
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
            except Exception as error:
                result = {"error": str(error).split(":", 1)[0]}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
