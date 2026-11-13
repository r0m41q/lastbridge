from lastbridge.shared.recordings import recording_key

POLICIES = {
    "comfort": ("Comfort", "Familiar favorites and six-month top tracks."),
    "discover": (
        "Discover",
        "Similar tracks and artist-based suggestions outside your known "
        "listening sample; charts for new profiles.",
    ),
    "again": (
        "Listen Again",
        "Recent local reports followed by Last.fm listening.",
    ),
    "loved": (
        "Loved Tracks",
        "Matched Last.fm loves still saved in your library.",
    ),
}
DAY = 86400
COOLDOWN = 300
LOOKUPS = 40
DEADLINE = 180


def key(row):
    return recording_key(*row)


def unique(rows):
    seen, result = set(), []
    for row in rows:
        identity = key(row)
        if identity not in seen:
            seen.add(identity)
            result.append(tuple(row))
    return result
