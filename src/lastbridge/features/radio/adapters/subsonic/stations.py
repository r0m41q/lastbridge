from lastbridge.shared.errors import CatalogError
from lastbridge.transport.http.context import services
from lastbridge.transport.subsonic.parameters import required


async def stations(request, method, values, user):
    station_list = services(request).stations
    if method == "getInternetRadioStations":
        return {
            "internetRadioStations": {
                "internetRadioStation": station_list.list()
            }
        }
    if request.method == "HEAD":
        raise CatalogError(10, "Use GET or POST to edit radio stations")
    station_list.administrator(user)
    if method == "deleteInternetRadioStation":
        station_list.remove(user, required(values, "id"))
    else:
        item_id = (
            required(values, "id")
            if method == "updateInternetRadioStation"
            else None
        )
        station_list.save(user, values, item_id)
    return {}


HANDLERS = {
    name: stations
    for name in (
        "getInternetRadioStations",
        "createInternetRadioStation",
        "updateInternetRadioStation",
        "deleteInternetRadioStation",
    )
}
