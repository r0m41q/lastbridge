from lastbridge.transport.http.context import services


def record_activity(request, method, values, user):
    # Routine client sync, ping and status polling are not evidence of use.
    active = method in {
        "stream",
        "download",
        "scrobble",
        "star",
        "unstar",
        "createPlaylist",
        "updatePlaylist",
        "deletePlaylist",
        "getArtist",
        "getAlbum",
        "getTopSongs",
        "getSimilarSongs",
        "getSimilarSongs2",
    } or (
        method in {"search2", "search3"}
        and values.get("query", "").strip().strip('"')
    )
    if active and request.method != "HEAD":
        services(request).mixes.touch(user["id"])
