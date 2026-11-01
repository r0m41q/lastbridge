from lastbridge.shared.errors import CatalogError
from lastbridge.transport.http.context import services


async def report(request, method, values, user):
    user_id = user["id"]
    if request.method == "HEAD":
        raise CatalogError(10, "Use GET or POST to report playback")
    services(request).sync.report(user, values)
    if values.get("submission") == "false" and values.get("id"):
        services(request).radio.warm(user_id, values["id"][-1])
    return {}


HANDLERS = {
    "scrobble": report,
}
