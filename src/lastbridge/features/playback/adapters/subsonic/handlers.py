from lastbridge.shared.errors import CatalogError
from lastbridge.transport.subsonic.parameters import integer, required


async def audio(request, method, values, user):
    catalog = request.app.state.catalog
    user_id = user["id"]
    song = catalog.read(required(values, "id"), "song")
    # Preserve the configured source audio. Clients seek with HTTP Range;
    # no transcoding or transcode-offset extension is advertised.
    if method == "stream":
        integer(values, "maxBitRate", 0)
        if values.get("timeOffset", "0") not in ("0", "0.0"):
            raise CatalogError(0, "Use HTTP Range to seek source audio")
    if method == "stream" and request.method != "HEAD":
        request.app.state.radio.warm(user_id, song["id"])
    return await request.app.state.audio.response(
        song["source"], download=method == "download"
    )


HANDLERS = {
    "stream": audio,
    "download": audio,
}
