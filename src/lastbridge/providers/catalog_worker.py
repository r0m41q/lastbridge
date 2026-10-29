import contextlib
import json
import logging
import re
import sys

from lastbridge.providers.youtube import normalize, normalize_title


def audio_row(row):
    if not isinstance(row, dict):
        return False
    album = row.get("album")
    return (
        isinstance(row.get("videoId"), str)
        and re.fullmatch(r"[A-Za-z0-9_-]{11}", row["videoId"]) is not None
        and row.get("videoType") == "MUSIC_VIDEO_TYPE_ATV"
        and row.get("isAvailable") is not False
        and isinstance(album, dict)
        and isinstance(album.get("id"), str)
        and album["id"].startswith("MPRE")
        and isinstance(album.get("name"), str)
        and bool(album["name"])
        and isinstance(row.get("title"), str)
        and 0 < len(row["title"]) <= 1024
        and isinstance(row.get("artists"), list)
    )


def radio_tracks(music, source):
    result = music.get_watch_playlist(videoId=source, radio=True, limit=50)
    rows = result["tracks"][:100]
    if any(audio_row(row) for row in rows):
        return rows
    # A music-video seed can return only music videos, without albums. Find
    # an exact official-audio recommendation anchor instead of publishing an
    # empty queue. Playback still uses the originally selected recording.
    seed = next(
        (
            r
            for r in rows
            if isinstance(r, dict) and r.get("videoId") == source
        ),
        None,
    )
    if seed is None or not isinstance(seed.get("title"), str):
        return rows
    artists = seed.get("artists")
    if not isinstance(artists, list) or not artists:
        return rows
    first = artists[0]
    if not isinstance(first, dict) or not isinstance(first.get("name"), str):
        return rows
    artist, title = first["name"], seed["title"]
    if not 0 < len(artist) <= 1024 or not 0 < len(title) <= 1024:
        return rows
    candidates = music.search(f"{artist} {title}", filter="songs", limit=20)
    for candidate in candidates[:20]:
        if not audio_row(candidate):
            continue
        names = {
            normalize(a["name"])
            for a in candidate["artists"]
            if isinstance(a, dict) and isinstance(a.get("name"), str)
        }
        if normalize(artist) not in names or normalize_title(
            title
        ) != normalize_title(candidate["title"]):
            continue
        # Provider relevance chooses among same-name album releases. Never
        # strip live/remix/version qualifiers or substitute another performer.
        return music.get_watch_playlist(
            videoId=candidate["videoId"], radio=True, limit=50
        )["tracks"][:100]
    return rows


def execute(payload):
    from ytmusicapi import YTMusic

    music = YTMusic()
    operation = payload["operation"]
    if operation == "search":
        return {
            kind: music.search(payload["query"], filter=kind, limit=100)[:100]
            for kind in payload["kinds"]
        }
    if operation == "album":
        return music.get_album(payload["source"])
    if operation == "artist":
        return music.get_artist(payload["source"])
    if operation == "radio":
        tracks = radio_tracks(music, payload["source"])
        # Only public metadata crosses the worker boundary. Ignore tokens,
        # counterparts, browse links and provider playlist/session state.
        return {
            "tracks": [
                {
                    key: row[key]
                    for key in (
                        "videoId",
                        "title",
                        "artists",
                        "album",
                        "videoType",
                        "length",
                        "duration_seconds",
                        "thumbnail",
                        "isAvailable",
                    )
                    if key in row
                }
                for row in tracks
                if isinstance(row, dict)
            ]
        }
    raise ValueError("Unknown operation")


def main():
    logging.disable(logging.CRITICAL)
    payload = json.load(sys.stdin)
    # ytmusicapi does not normally print; reserve stdout for the result even
    # when a future dependency adds progress output.
    # Discard any unexpected dependency output without retaining it in RAM.
    import os

    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
        try:
            result = {"data": execute(payload)}
        except Exception:
            result = {"error": "Metadata provider unavailable"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
