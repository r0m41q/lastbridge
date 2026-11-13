from fastapi.responses import RedirectResponse, Response

from lastbridge.shared.errors import CatalogError
from lastbridge.transport.http.context import services
from lastbridge.transport.subsonic.parameters import integer, required
from lastbridge.transport.subsonic.serialization import directory_album


async def top_songs(request, method, values, user):
    catalog = services(request).catalog
    library = services(request).library
    user_id = user["id"]
    if "id" in values:
        required(values, "id")
    else:
        required(values, "artist")
    count = min(integer(values, "count", 50), 50)
    songs = await catalog.top_songs(
        user_id,
        values.get("id"),
        values.get("artist"),
        fetch=request.method != "HEAD" and count > 0,
    )
    return {"topSongs": {"song": library.decorate(user_id, songs[:count])}}


async def artist_info(request, method, values, user):
    catalog = services(request).catalog
    count = min(integer(values, "count", 20), 50)
    present = values.get("includeNotPresent", "false")
    if present not in ("true", "false"):
        raise CatalogError(10, "Invalid parameter: includeNotPresent")
    info, names = await catalog.artist_info(
        required(values, "id"), fetch=request.method != "HEAD"
    )
    info["similarArtist"] = catalog.similar_artists(
        names, count, present == "true"
    )
    return {
        "artistInfo2" if method.endswith("2") else "artistInfo": services(
            request
        ).library.decorate(user["id"], info)
    }


async def search(request, method, values, user):
    catalog = services(request).catalog
    library = services(request).library
    user_id = user["id"]
    if "query" not in values:
        raise CatalogError(10, "Required parameter: query")
    query = values["query"].strip()
    if query == '""':
        query = ""
    if len(query) > 256:
        raise CatalogError(10, "Search query too long")
    pages = {
        kind: (
            integer(values, kind + "Offset", 0),
            min(integer(values, kind + "Count", 20), 100 if query else 500),
        )
        for kind in ("artist", "album", "song")
    }
    kinds = [
        k + "s"
        for k, (offset, count) in pages.items()
        if count and offset < 100
    ]
    # Empty query is the user's synchronizable saved library. Search
    # metadata is public internally but must never populate that library.
    results = (
        await catalog.search(user["id"], query, kinds)
        if (query and kinds)
        else library.collection(user_id)
        if not query
        else {"artist": [], "album": [], "song": []}
    )
    result = {}
    for kind, (offset, count) in pages.items():
        stop = offset + count
        result[kind] = results[kind][offset:stop]
    if method == "search2":
        result["album"] = [directory_album(a) for a in result["album"]]
    if query:
        result = library.decorate(user_id, result)
    return {
        "searchResult2" if method == "search2" else "searchResult3": result
    }


async def detail(request, method, values, user):
    catalog = services(request).catalog
    library = services(request).library
    user_id = user["id"]
    kind = method[3:].lower()
    return {
        kind: library.decorate(
            user_id, await catalog.detail(required(values, "id"), kind)
        )
    }


async def cover(request, method, values, user):
    catalog = services(request).catalog
    if "size" in values:
        integer(values, "size", 0, 4096)
    item_id = required(values, "id")
    if item_id.startswith("station-"):
        url = services(request).stations.artwork_url(item_id)
        # POST clients must not forward their authentication form to the host.
        return RedirectResponse(url, status_code=303)
    data, media_type = await catalog.artwork(
        services(request).http_client, item_id
    )
    # Google artwork is requested at 1080px; the client's size is advisory.
    return Response(data, media_type=media_type)


HANDLERS = {
    "getTopSongs": top_songs,
    "getArtistInfo": artist_info,
    "getArtistInfo2": artist_info,
    "search2": search,
    "search3": search,
    "getSong": detail,
    "getAlbum": detail,
    "getArtist": detail,
    "getCoverArt": cover,
}
