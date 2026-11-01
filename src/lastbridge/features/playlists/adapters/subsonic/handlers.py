from lastbridge.shared.errors import CatalogError
from lastbridge.transport.http.context import services
from lastbridge.transport.subsonic.parameters import required


async def lists(request, method, values, user):
    if values.get("username", user["username"]) != user["username"]:
        raise CatalogError(50, "Access denied")
    playlists = services(request).mixes.playlists(user)
    playlists += services(request).playlists.local(user)
    radio = services(request).radio.playlist(user)
    return {"playlists": {"playlist": playlists + ([radio] if radio else [])}}


async def detail(request, method, values, user):
    return {
        "playlist": services(request).playlists.get(
            user, required(values, "id")
        )
    }


async def edit(request, method, values, user):
    if request.method == "HEAD":
        raise CatalogError(10, "Use GET or POST to edit playlists")
    playlists = services(request).playlists
    if method == "createPlaylist":
        if "playlistId" in values and not values["playlistId"]:
            raise CatalogError(10, "Required parameter: playlistId")
        return {
            "playlist": await playlists.create(
                user,
                values.get("name"),
                values.get("songId", []),
                values.get("playlistId"),
            )
        }
    item_id = required(
        values, "playlistId" if method == "updatePlaylist" else "id"
    )
    if method == "updatePlaylist":
        await playlists.update(user, item_id, values)
    else:
        await playlists.delete(user, item_id)
    return {}


HANDLERS = {
    "getPlaylists": lists,
    "getPlaylist": detail,
    "createPlaylist": edit,
    "updatePlaylist": edit,
    "deletePlaylist": edit,
}
