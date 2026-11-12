from datetime import UTC, datetime

from lastbridge.transport.browser.rendering import Page, document
from lastbridge.transport.browser.review import status


def review_model(item, user):
    entries = (
        item["progress"].get("entries", [])
        if not item["ready"] and item["state"] != "complete"
        else item["entries"]
    )
    path = "/playlist-imports/" + item["id"]
    rows = []
    for entry in entries:
        state, label = status(entry)
        rows.append(
            entry
            | {
                "key": entry["source_key"],
                "status": state,
                "label": label,
                "path": path,
                "kind": "playlist",
            }
        )
    return {
        "entries": rows,
        "path": path,
        "kind": "playlist",
        "scope": "Shared" if item["shared"] else "Private",
        "editable": not item["shared"] or bool(user["admin"]),
        "running": item["state"] in ("queued", "running"),
        "header": import_header(item, user, entries),
        "progress": {
            key: item[key]
            for key in ("state", "processed", "total", "matched")
        },
    }


def import_header(item, user, entries):
    return document(
        "playlists/import_header.html",
        Page("Playlist import"),
        user=user,
        item=item,
        playable=sum(bool(e.get("song_id")) for e in entries),
        entry_count=len(entries),
        updated=datetime.fromtimestamp(item["updated"], UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
        next_refresh=datetime.fromtimestamp(
            item["next_attempt"], UTC
        ).strftime("%Y-%m-%d %H:%M UTC"),
    )
