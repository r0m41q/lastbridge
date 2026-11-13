from lastbridge.features.discovery.domain.policy import key, unique
from lastbridge.shared.recordings import normalize


async def discover(read, seeds, artists, excluded):
    """Expand a bounded taste sample when similar tracks are sparse."""
    groups = []

    def suggestions():
        return [
            row
            for row in unique(
                [
                    rows[i]
                    for i in range(50)
                    for rows in groups
                    if i < len(rows)
                ]
            )
            if key(row) not in excluded
        ]

    for artist, title in seeds[:6]:
        groups.append(
            await read(
                "track.getSimilar",
                artist=artist,
                track=title,
                autocorrect="1",
            )
        )
        if len(suggestions()) >= 30:
            break
    if len(suggestions()) < 30:
        # Not every track has similarity data. Related artists offer another
        # source without replacing a listener's taste with global charts.
        names, seen = [], set()
        for artist, _ in artists + seeds:
            identity = normalize(artist)
            if identity not in seen:
                seen.add(identity)
                names.append(artist)
        neighbours = []
        for artist in names[:2]:
            neighbours.extend(await read("artist.getSimilar", artist=artist))
        expanded = []
        seen.clear()
        for artist in [a for a, _ in neighbours] + names:
            identity = normalize(artist)
            if identity not in seen:
                seen.add(identity)
                expanded.append(artist)
        for artist in expanded[:3]:
            groups.append(await read("artist.getTopTracks", artist=artist))
            if len(suggestions()) >= 30:
                break
    if not seeds and not artists:
        groups.append(await read("chart.getTopTracks"))
    return suggestions()[:60]
