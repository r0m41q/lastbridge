from lastbridge.shared.errors import CatalogError
from lastbridge.transport.subsonic.parameters import required


async def ping(request, method, values, user):
    return {}


async def license(request, method, values, user):
    return {"license": {"valid": True}}


async def user(request, method, values, user):
    name = required(values, "username")
    # Account management lives in the browser. Even browser administrators
    # cannot enumerate another user's protocol profile here.
    if name != user["username"]:
        raise CatalogError(50, "Access denied")
    return {
        "user": {
            "username": name,
            "folder": [1],
            "scrobblingEnabled": bool(
                request.app.state.lastfm.configured
                and user["link_status"] == "linked"
                and user["lastfm_secret"]
            ),
            **{
                role + "Role": False
                for role in (
                    "admin",
                    "settings",
                    "upload",
                    "comment",
                    "podcast",
                    "jukebox",
                    "share",
                    "video",
                )
            },
            "coverArtRole": True,
            "playlistRole": True,
            "streamRole": True,
            "downloadRole": True,
        }
    }


HANDLERS = {
    "ping": ping,
    "getLicense": license,
    "getUser": user,
}
