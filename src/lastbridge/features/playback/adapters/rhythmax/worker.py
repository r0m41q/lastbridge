import asyncio
import contextlib
import json
import logging
import math
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin

import httpx

from lastbridge.features.playback.adapters.rhythmax.client import (
    RhythmaxClient,
    RhythmaxError,
)
from lastbridge.shared.recordings import normalize, normalize_title


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element, name: str):
    return next(
        (child for child in list(element) if _local_name(child.tag) == name),
        None,
    )


def _artist(row: dict) -> str:
    value = row.get("artist")
    if isinstance(value, dict) and isinstance(value.get("name"), str):
        return value["name"]
    artists = row.get("artists")
    if isinstance(artists, list) and artists:
        first = artists[0]
        if isinstance(first, dict) and isinstance(first.get("name"), str):
            return first["name"]
    return ""


def choose_candidate(rows: list[dict], recording: dict) -> str | None:
    artist = normalize(recording.get("artist", ""))
    title = normalize_title(recording.get("title", ""))
    album = normalize(recording.get("album", ""))
    duration = recording.get("duration")
    candidates = []
    for row in rows:
        if not isinstance(row, dict) or row.get("allowStreaming") is False:
            continue
        track_id = str(row.get("id", ""))
        row_title = row.get("title")
        if not track_id or not isinstance(row_title, str):
            continue
        if normalize(_artist(row)) != artist:
            continue
        if normalize_title(row_title) != title:
            continue
        release = row.get("album")
        release_name = (
            normalize(release.get("title", ""))
            if isinstance(release, dict)
            else ""
        )
        if album and release_name != album:
            continue
        try:
            duration_delta = abs(
                float(row.get("duration", 0)) - float(duration or 0)
            )
        except (TypeError, ValueError):
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


async def resolve(
    api: RhythmaxClient, recording: dict
) -> str:
    query = " ".join(
        str(recording.get(key, ""))
        for key in ("artist", "title")
        if recording.get(key)
    )
    if not query:
        raise RhythmaxError("no_match")
    payload = await api.search(query)
    track_id = choose_candidate(payload.get("items", []), recording)
    if track_id is None:
        raise RhythmaxError("no_match")
    return track_id


def parse_manifest(manifest_url: str, body: str) -> tuple[str, list[str]]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise RhythmaxError("invalid_manifest") from error

    for adaptation in root.iter():
        if _local_name(adaptation.tag) != "AdaptationSet":
            continue
        representations = [
            child
            for child in list(adaptation)
            if _local_name(child.tag) == "Representation"
        ]
        for representation in representations:
            template = _child(representation, "SegmentTemplate")
            if template is None:
                template = _child(adaptation, "SegmentTemplate")
            if template is None:
                continue
            initialization = template.get("initialization")
            media = template.get("media")
            timeline = _child(template, "SegmentTimeline")
            if not initialization or not media or timeline is None:
                continue
            if "$Time$" in initialization or "$Time$" in media:
                raise RhythmaxError("unsupported_manifest")
            representation_id = representation.get("id", "")
            base = manifest_url
            for parent in (root, adaptation, representation):
                base_element = _child(parent, "BaseURL")
                if base_element is not None and base_element.text:
                    base = urljoin(base, base_element.text.strip())
            initialization_url = urljoin(
                base,
                initialization.replace("$RepresentationID$", representation_id)
                .replace("$Number$", "0"),
            )
            number = int(template.get("startNumber", "1"))
            segments = []
            for segment in list(timeline):
                if _local_name(segment.tag) != "S":
                    continue
                repeat = int(segment.get("r", "0"))
                if repeat < 0:
                    raise RhythmaxError("unsupported_manifest")
                for _ in range(repeat + 1):
                    segments.append(
                        urljoin(
                            base,
                            media.replace(
                                "$RepresentationID$", representation_id
                            ).replace("$Number$", str(number)),
                        )
                    )
                    number += 1
            if segments:
                return (
                    representation.get("codecs", ""),
                    [initialization_url, *segments],
                )
    raise RhythmaxError("invalid_manifest")


async def download(payload: dict) -> dict:
    directory = Path(payload["directory"])
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True
    ) as http:
        api = RhythmaxClient(http)
        track_id = await resolve(api, payload["recording"])
        manifest = await api.manifest(track_id, "LOSSLESS")
        data = manifest.get("data", {})
        attributes = data.get("attributes")
        if not isinstance(attributes, dict):
            nested = data.get("data")
            attributes = (
                nested.get("attributes")
                if isinstance(nested, dict)
                else None
            )
        if not isinstance(attributes, dict) or not attributes.get("uri"):
            raise RhythmaxError("manifest_unavailable")
        if attributes.get("trackPresentation") == "PREVIEW":
            raise RhythmaxError("preview_only")
        manifest_url = str(attributes["uri"])
        try:
            response = await http.get(manifest_url)
        except httpx.HTTPError as error:
            raise RhythmaxError("manifest_unavailable") from error
        if response.status_code != 200:
            raise RhythmaxError("manifest_unavailable", response.status_code)
        _, urls = parse_manifest(manifest_url, response.text)
        proxies = await api.proxy_urls()
        source = directory / "rhythmax-source.mp4"
        try:
            with source.open("wb") as output:
                for url in urls:
                    if shutil.disk_usage(directory).free < payload["reserve"]:
                        raise RhythmaxError("storage_full")
                    chunk = await api.fetch_segment(url, proxies)
                    output.write(chunk)
            extension = "m4a" if payload["policy"] == "aac" else "webm"
            destination = directory / f"audio.{extension}"
            if payload["policy"] == "aac":
                codec = ["-c:a", "aac", "-b:a", "320k", "-f", "ipod"]
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
