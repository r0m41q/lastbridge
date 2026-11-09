"""Recommendations from YouTube Music's prediction dropdown."""

import re
from functools import partial

from lastbridge.features.catalog.domain.documents import number


def browse(run):
    endpoint = run.get("navigationEndpoint", {}).get("browseEndpoint", {})
    source = endpoint.get("browseId")
    name = run.get("text")
    if (
        isinstance(source, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", source)
        and isinstance(name, str)
        and 0 < len(name) <= 1024
    ):
        return {"id": source, "name": name}
    return None


def parse_item(renderer):
    columns = [
        c.get("musicResponsiveListItemFlexColumnRenderer", {})
        .get("text", {})
        .get("runs", [])
        for c in renderer.get("flexColumns", [])[:4]
    ]
    if not columns or not columns[0]:
        return None
    title = columns[0][0].get("text")
    if not isinstance(title, str) or not 0 < len(title) <= 1024:
        return None
    endpoint = renderer.get("navigationEndpoint", {})
    watch = endpoint.get("watchEndpoint", {})
    source = watch.get("videoId")
    video_type = (
        watch.get("watchEndpointMusicSupportedConfigs", {})
        .get("watchEndpointMusicConfig", {})
        .get("musicVideoType")
    )
    links = [
        item
        for column in columns[1:]
        for run in column
        if (item := browse(run))
    ]
    artists = [link for link in links if link["id"].startswith("UC")]
    albums = [link for link in links if link["id"].startswith("MPRE")]
    thumbnails = (
        renderer.get("thumbnail", {})
        .get("musicThumbnailRenderer", {})
        .get("thumbnail", {})
        .get("thumbnails", [])
    )
    if (
        isinstance(source, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{11}", source)
        and video_type == "MUSIC_VIDEO_TYPE_ATV"
        and artists
        and albums
    ):
        return "songs", {
            "videoId": source,
            "title": title,
            "videoType": video_type,
            "artists": artists,
            "album": albums[0],
            "thumbnails": thumbnails,
        }
    entity = browse({"text": title, "navigationEndpoint": endpoint})
    if entity and entity["id"].startswith("UC"):
        return "artists", entity | {"thumbnails": thumbnails}
    if entity and entity["id"].startswith("MPRE"):
        return "albums", {
            "browseId": entity["id"],
            "title": title,
            "artists": artists,
            "thumbnails": thumbnails,
        }
    return None


def recommendations(response):
    result = {"songs": [], "artists": [], "albums": []}
    # Section positions vary; text predictions and history are not entities.
    for section in response.get("contents", [])[:5]:
        entries = section.get("searchSuggestionsSectionRenderer", {}).get(
            "contents", []
        )
        for entry in entries[:20]:
            renderer = entry.get("musicResponsiveListItemRenderer")
            if not isinstance(renderer, dict):
                continue
            try:
                parsed = parse_item(renderer)
            except AttributeError, TypeError, KeyError:
                continue
            if parsed:
                kind, row = parsed
                if len(result[kind]) < 5:
                    result[kind].append(row)
    return result


def search(music, query, kinds):
    result = {
        kind: music.search(query, filter=kind, limit=100)[:100]
        for kind in kinds
    }
    if not kinds:
        return result
    # ytmusicapi's public suggestion helper discards recommended entities.
    # Bound these optional calls; an unavailable dropdown must not erase the
    # regular results. The worker's overall deadline remains the outer bound.
    request = music._session.request
    music._session.request = partial(request, timeout=3)
    try:
        try:
            suggested = recommendations(
                music._send_request(
                    "music/get_search_suggestions", {"input": query}
                )
            )
        except Exception:
            return result
        for kind in kinds:
            rows = []
            seen = set()
            key = "videoId" if kind == "songs" else "id"
            existing = {
                row.get(key) or row.get("browseId"): row
                for row in result[kind]
            }
            for row in suggested.get(kind, []):
                source = row.get(key) or row.get("browseId")
                if source in seen:
                    continue
                if source in existing:
                    row = existing[source] | row
                if kind == "songs" and not row.get("duration_seconds"):
                    try:
                        details = music.get_song(source).get(
                            "videoDetails", {}
                        )
                        duration = number(details.get("lengthSeconds"))
                        if details.get("videoId") != source or not duration:
                            continue
                        row = row | {"duration_seconds": duration}
                    except Exception:
                        continue
                rows.append(row)
                seen.add(source)
            for row in result[kind]:
                source = row.get(key) or row.get("browseId")
                if source not in seen:
                    rows.append(row)
                    seen.add(source)
            result[kind] = rows[:100]
        return result
    finally:
        music._session.request = request
