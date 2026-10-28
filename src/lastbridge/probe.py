import argparse
import json
import math
import platform
import shutil
import time
from dataclasses import asdict
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from lastbridge.providers.base import MusicProvider, ProviderError, Recording
from lastbridge.providers.youtube import YouTubeProvider, match_recording

SUITE = (
    Recording("Daft Punk", "Instant Crush", "Random Access Memories", 337),
    Recording("Joji", "PIXELATED KISSES", "Piss In The Wind", 111),
    Recording("OTOY", "Судоми", "Судоми", 147),
)

HINTS = {
    "dependencies_missing": "Install the optional probe dependency group.",
    "network_error": "Check DNS, TLS, proxy, and deployment-network access.",
    "access_denied": "Anonymous access was refused; review provider setup.",
    "unavailable": "Recording is unavailable or restricted on this network.",
    "no_match": "Inspect candidates; specify exact album/version/duration.",
    "ambiguous_match": "Specify album/duration to disambiguate versions.",
    "no_audio_format": "No direct AAC/M4A or Opus/WebM audio was available.",
    "timeout": "Worker and its media subprocesses hit the deadline.",
    "sample_failed": "Sample fetch/probe/decode failed; delivery is unproven.",
    "sample_access_denied": "Audio CDN refused the anonymous sample request.",
    "sample_network_error": "Audio CDN connection failed; check the network.",
    "sample_fetch_failed": "FFmpeg could not fetch/remux the audio sample.",
    "sample_exists": "Choose a fresh sample directory; existing audio is kept",
    "media_tools_missing": "Install FFmpeg and ffprobe on the probe host.",
}


def probe_one(
    provider: MusicProvider,
    recording: Recording | None,
    source_id: str | None,
    sample_dir: Path | None,
) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "recording": asdict(recording) if recording else None,
        "matching": "bypassed_for_explicit_id" if source_id else "required",
    }
    try:
        if recording:
            candidates = provider.search(recording)
            result["search_seconds"] = round(time.monotonic() - started, 3)
            result["candidates"] = [asdict(item) for item in candidates]
            selected = match_recording(recording, candidates)
            source_id = selected.source_id
        if source_id is None:
            raise ProviderError("invalid_source_id")
        result["source_id"] = source_id
        result.update(provider.inspect(source_id, sample_dir))
        if result.get("sample_error"):
            raise ProviderError(result["sample_error"])
        result["status"] = "sample_verified" if sample_dir else "resolved_only"
    except ProviderError as error:
        code = str(error)
        result.update(
            status="failed",
            error=code,
            hint=HINTS.get(
                code, "Provider failed; no delivery claim is made."
            ),
        )
    result["total_seconds"] = round(time.monotonic() - started, 3)
    return result


def positive_seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0 < number <= 120:
        raise argparse.ArgumentTypeError(
            "must be greater than 0 and at most 120"
        )
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="allow provider calls"
    )
    parser.add_argument(
        "--suite", action="store_true", help="three test recordings"
    )
    parser.add_argument("--artist")
    parser.add_argument("--title")
    parser.add_argument("--album")
    parser.add_argument("--duration", type=float)
    parser.add_argument(
        "--source-id", help="diagnostic only; bypasses matching"
    )
    parser.add_argument("--timeout", type=positive_seconds, default=45)
    parser.add_argument(
        "--js-runtime", choices=("node", "deno"), default="node"
    )
    parser.add_argument(
        "--player-client",
        choices=("default", "visionos"),
        default="visionos",
        help="compare yt-dlp defaults with the reference resolver's client",
    )
    parser.add_argument(
        "--sample-dir", type=Path, help="save 10-second audio clips"
    )
    parser.add_argument("--report", type=Path, help="write JSON to a new file")
    args = parser.parse_args()
    if not args.live:
        parser.error("provider calls require explicit --live")
    modes = sum(
        (args.suite, bool(args.source_id), bool(args.artist or args.title))
    )
    if modes != 1 or (bool(args.artist) != bool(args.title)):
        parser.error(
            "choose --suite, --source-id, or both --artist and --title"
        )
    if (args.album or args.duration is not None) and not args.artist:
        parser.error("--album/--duration require --artist and --title")
    if args.duration is not None and (
        not math.isfinite(args.duration) or args.duration <= 0
    ):
        parser.error("--duration must be a finite positive number")
    if args.report and args.report.exists():
        parser.error("report already exists; choose a new path")
    versions = {"python": platform.python_version()}
    for package in ("ytmusicapi", "yt-dlp", "yt-dlp-ejs"):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = "not_installed"
    provider = YouTubeProvider(
        args.timeout, args.js_runtime, args.player_client
    )
    recordings = (
        SUITE
        if args.suite
        else (Recording(args.artist, args.title, args.album, args.duration),)
        if args.artist
        else (None,)
    )
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "versions": versions,
        "anonymous": True,
        "timeout_per_stage_seconds": args.timeout,
        "js_runtime": args.js_runtime,
        "player_client": args.player_client,
        "tools_present": {
            tool: bool(shutil.which(tool))
            for tool in (args.js_runtime, "ffmpeg", "ffprobe")
        },
        "results": [],
    }
    try:
        for recording in recordings:
            report["results"].append(
                probe_one(provider, recording, args.source_id, args.sample_dir)
            )
        output = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open("x") as destination:
                destination.write(output)
        print(output, end="")
    except OSError:
        parser.exit(
            1, "Probe filesystem/tool error; check writable output paths.\n"
        )
    raise SystemExit(
        any(row["status"] == "failed" for row in report["results"])
    )


if __name__ == "__main__":
    main()
