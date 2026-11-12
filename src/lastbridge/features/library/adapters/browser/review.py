"""Library review presentation; no playlist-specific save behavior."""


def queue_model(corrections, user_id, selected=None):
    entries = []
    for row in corrections.queue(user_id):
        skipped = bool(row["review_skipped"])
        state = (
            "skipped"
            if skipped
            else ("resolved" if row["state"] == "matched" else "review")
        )
        key = row["recording_key"]
        entries.append(
            {
                "key": key,
                "kind": "love",
                "artist": row["artist"],
                "title": row["title"],
                "song_id": row["song_id"],
                "status": state,
                "label": {
                    "skipped": "Skipped",
                    "resolved": "Confirmed",
                    "review": "Choose recording",
                }[state],
                "reason": row["error"],
                "skipped": skipped,
                "path": "/library/recordings/love/" + key,
                "candidates": [],
                "loaded": False,
            }
        )
    return {
        "kind": "love",
        "entries": entries,
        "editable": True,
        "running": False,
        "path": "/library/recordings/loves",
        "selected": selected,
    }


def recording_entry(item, path):
    current = item["current"]
    skipped = bool(item.get("review_skipped"))
    return {
        "key": item["key"],
        "kind": item["kind"],
        "path": path,
        "artist": ", ".join(a["name"] for a in item["source"]["artists"]),
        "title": item["source"]["title"],
        "duration": item["source"].get("duration_seconds"),
        "explicit": item["source"].get("isExplicit"),
        "status": "skipped"
        if skipped
        else ("resolved" if item["kind"] == "love" and current else "review"),
        "label": "Skipped"
        if skipped
        else (
            "Confirmed"
            if item["kind"] == "love" and current
            else "Choose recording"
        ),
        "skipped": skipped,
        "song_id": item.get("song_id"),
        "current": current["document"] if current else None,
        "can_clear": bool(
            current
            and item["kind"] == "song"
            and current["recording_id"] != item["original"]["id"]
        ),
        "candidates": item["review"]["candidates"] if item["review"] else [],
        "token": item["review"]["token"] if item["review"] else "",
        "searched": bool(item["review"]),
        "loaded": True,
    }
