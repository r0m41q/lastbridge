from lastbridge.shared.errors import CatalogError, StorageError
from lastbridge.transport.http.context import services
from lastbridge.transport.subsonic.parameters import integer


async def queue(request, method, values, user):
    service = services(request).play_queue
    try:
        if method == "getPlayQueue":
            document = service.get(user["id"])
            document["entry"] = services(request).library.decorate(
                user["id"], document["entry"]
            )
            return {"playQueue": document}
        if request.method == "HEAD":
            raise CatalogError(10, "Use GET or POST to save the play queue")
        position = integer(
            values | {"position": values.get("position") or "0"},
            "position",
            0,
            maximum=2**63 - 1,
        )
        service.save(
            user,
            values.get("id", []),
            values.get("current"),
            position,
            values.get("c", ""),
        )
        return {}
    except StorageError:
        raise CatalogError(
            0, "Play queue storage unavailable; retry"
        ) from None


HANDLERS = {"savePlayQueue": queue, "getPlayQueue": queue}
