from lastbridge.shared.dates import date

DESCRIPTIONS = (
    ("comfort", "Favourites and top tracks."),
    ("discover", "Last.fm similar-track recommendations."),
    ("again", "Recently played tracks."),
    ("loved", "Imported Last.fm loved tracks."),
)


def mixes_view(mixes, user):
    status = mixes.status(user["id"])
    items = mixes.playlists(user)
    return {
        "cards": [
            {
                "kind": kind,
                "description": description,
                "name": item["name"],
                "count": item["songCount"],
            }
            for item, (kind, description) in zip(
                items, DESCRIPTIONS, strict=True
            )
        ],
        "status": status,
        "refresh_hours": mixes.refresh_interval // 3600,
        "inactive_days": mixes.inactive_ttl // 86400,
        "state": "refreshing"
        if status and status["running"]
        else "waiting for next refresh",
        "last": date(status["updated"])
        if status and status["updated"]
        else "Not yet",
        "next_time": date(status["next_attempt"])
        if status and status["next_attempt"]
        else "Queued",
    }
