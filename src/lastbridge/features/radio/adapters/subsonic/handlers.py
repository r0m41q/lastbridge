from lastbridge.shared.errors import CatalogError
from lastbridge.transport.http.context import services
from lastbridge.transport.subsonic.parameters import integer, required


async def similar(request, method, values, user):
    if request.method == "HEAD":
        raise CatalogError(10, "Use GET or POST to select radio")
    songs = await services(request).radio.songs(
        user,
        required(values, "id"),
        min(integer(values, "count", 50), 50),
    )
    return {
        "similarSongs2" if method.endswith("2") else "similarSongs": {
            "song": songs
        }
    }


HANDLERS = {
    "getSimilarSongs": similar,
    "getSimilarSongs2": similar,
}
