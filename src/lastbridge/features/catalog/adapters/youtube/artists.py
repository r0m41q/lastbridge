RELEASE_LIMIT = 1000


def artist_releases(music, source):
    row = music.get_artist(source)
    if not isinstance(row, dict) or not row.get("name"):
        raise ValueError("Invalid artist response")
    complete = True
    for group in ("albums", "singles"):
        shelf = row.get(group)
        if shelf is None:
            continue
        if not isinstance(shelf, dict):
            raise ValueError("Invalid release shelf")
        preview = shelf.get("results", [])
        if not isinstance(preview, list):
            raise ValueError("Invalid release preview")
        rows = preview
        browse_id, params = shelf.get("browseId"), shelf.get("params")
        if browse_id and params:
            try:
                # ytmusicapi follows continuation pages up to this limit.
                # One extra row distinguishes a full list from a capped one.
                rows = music.get_artist_albums(
                    browse_id, params, limit=RELEASE_LIMIT + 1
                )
                if not isinstance(rows, list):
                    raise ValueError("Invalid release list")
                # A missing preview item suggests a partial upstream response.
                ids = {r.get("browseId") for r in rows if isinstance(r, dict)}
                missing = [
                    r
                    for r in preview
                    if isinstance(r, dict) and r.get("browseId") not in ids
                ]
                if missing:
                    complete = False
                    rows = rows + missing
            except Exception:
                # Keep a useful page when just one shelf fails. Never mark
                # this preview as a successfully enumerated discography.
                rows, complete = preview, False
        elif browse_id or params:
            complete = False
        if len(rows) > RELEASE_LIMIT:
            complete = False
        row[group] = {"results": rows[:RELEASE_LIMIT]}
    return row | {"discography_complete": complete}
