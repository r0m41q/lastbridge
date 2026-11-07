import logging

from lastbridge.shared.errors import CatalogError
from lastbridge.transport.http.context import services
from lastbridge.transport.subsonic.parameters import integer, required

logger = logging.getLogger(__name__)


async def audio(request, method, values, user):
    catalog = services(request).catalog
    user_id = user["id"]
    try:
        song = catalog.read(required(values, "id"), "song")
        # Preserve the configured source audio. Clients seek with HTTP Range;
        # no transcoding or transcode-offset extension is advertised.
        if method == "stream":
            integer(values, "maxBitRate", 0)
            if values.get("timeOffset", "0") not in ("0", "0.0"):
                raise CatalogError(0, "Use HTTP Range to seek source audio")
        logger.info(
            "Audio request started method=%s source=%s user_id=%s",
            method,
            song["source"],
            user_id,
        )
        if method == "stream" and request.method != "HEAD":
            services(request).radio.warm(user_id, song["id"])
        return await services(request).audio.response(
            song["source"], download=method == "download"
        )
    except CatalogError as error:
        logger.warning(
            "Audio request failed method=%s user_id=%s code=%s reason=%s",
            method,
            user_id,
            error.code,
            error.message,
        )
        raise


HANDLERS = {
    "stream": audio,
    "download": audio,
}
