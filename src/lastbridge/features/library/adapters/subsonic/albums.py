import random

from lastbridge.shared.errors import CatalogError
from lastbridge.transport.http.context import services
from lastbridge.transport.subsonic.parameters import integer, required
from lastbridge.transport.subsonic.serialization import directory_album


async def albums(request, method, values, user):
    library = services(request).library
    user_id = user["id"]
    kind = required(values, "type")
    if kind not in (
        "random",
        "newest",
        "highest",
        "frequent",
        "recent",
        "starred",
        "alphabeticalByName",
        "alphabeticalByArtist",
        "byYear",
        "byGenre",
    ):
        raise CatalogError(10, "Invalid album list type")
    size = integer(values, "size", 10, 500)
    offset = integer(values, "offset", 0)
    if kind in ("recent", "frequent"):
        return album_response(
            method, library.played_albums(user_id, kind, size, offset)
        )
    collection = library.collection(user_id)
    albums = collection["album"]
    if kind == "byYear":
        start = integer(values, "fromYear", 0, 9999)
        end = integer(values, "toYear", 9999, 9999)
        albums = [
            a
            for a in albums
            if min(start, end) <= a.get("year", -1) <= max(start, end)
        ]
        albums.sort(key=lambda a: (a["year"], a["id"]), reverse=start > end)
    if kind == "byGenre":
        genre = required(values, "genre")
        albums = [
            a
            for a in albums
            if a.get("genre", "").casefold() == genre.casefold()
        ]
    if kind == "starred":
        albums = [a for a in albums if "starred" in a]
        albums.sort(key=lambda a: (a["starred"], a["id"]), reverse=True)
    elif kind == "newest":
        # A user's save time determines newest, never another user's first
        # insertion into the shared metadata catalog.
        stars = library.stars(user_id)
        saved_songs = collection["song"]
        albums.sort(
            key=lambda a: (
                max(
                    [stars.get(a["id"], "")]
                    + [
                        stars.get(s["id"], "")
                        for s in saved_songs
                        if s.get("albumId") == a["id"]
                    ]
                ),
                a["id"],
            ),
            reverse=True,
        )
    elif kind == "alphabeticalByArtist":
        albums.sort(
            key=lambda a: (
                a.get("artist", "").casefold(),
                a["name"].casefold(),
                a["id"],
            )
        )
    elif kind == "random":
        random.shuffle(albums)
    elif kind == "highest":
        # Album ratings remain unsupported.
        albums = []
    stop = offset + size
    albums = albums[offset:stop]
    return album_response(method, albums)


def album_response(method, albums):
    if method == "getAlbumList":
        albums = [directory_album(a) for a in albums]
    return {
        "albumList2" if method.endswith("2") else "albumList": {
            "album": albums
        }
    }


async def starred(request, method, values, user):
    library = services(request).library
    user_id = user["id"]
    result = library.collection(user_id, starred_only=True)
    if method == "getStarred":
        result["album"] = [directory_album(a) for a in result["album"]]
    return {"starred2" if method.endswith("2") else "starred": result}


HANDLERS = {
    "getAlbumList": albums,
    "getAlbumList2": albums,
    "getStarred": starred,
    "getStarred2": starred,
}
