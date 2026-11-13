from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from lastbridge.features.discovery.adapters.subsonic.activity import (
    record_activity,
)
from lastbridge.shared.errors import CatalogError
from lastbridge.transport.http.parameters import parameters
from lastbridge.transport.subsonic.authentication import authenticate
from lastbridge.transport.subsonic.methods import (
    HANDLERS,
    RADIO_STATION_METHODS,
)
from lastbridge.transport.subsonic.parameters import folder
from lastbridge.transport.subsonic.serialization import reply

router = APIRouter()
EXTENSIONS = [
    {"name": "apiKeyAuthentication", "versions": [1]},
    {"name": "formPost", "versions": [1]},
    {"name": "songLyrics", "versions": [1]},
    {"name": "topSongsByArtistId", "versions": [1]},
]


async def dispatch(request, method, values, user):
    folder(values)
    handler = HANDLERS.get(method)
    if handler is None:
        raise CatalogError(0, "Endpoint not implemented")
    return await handler(request, method, values, user)


@router.api_route("/rest/{method}", methods=["GET", "POST", "HEAD"])
async def endpoint(request: Request, method: str):
    method = method.removesuffix(".view")
    try:
        values = await parameters(
            request,
            multiple=frozenset({"id", "albumId", "artistId"})
            if method in ("star", "unstar")
            else frozenset({"id", "time"})
            if method == "scrobble"
            else frozenset({"id"})
            if method == "savePlayQueue"
            else frozenset({"songId", "songIdToAdd", "songIndexToRemove"})
            if method in ("createPlaylist", "updatePlaylist")
            else frozenset(),
            field_limit=2048
            if method in ("createPlaylist", "updatePlaylist", "savePlayQueue")
            else 128,
            body_limit=262144
            if method in ("createPlaylist", "updatePlaylist", "savePlayQueue")
            else None,
        )
    except HTTPException:
        return reply({}, 10, "Invalid request parameters")
    # The specification requires unauthenticated extension discovery. No user
    # state or upstream request is involved in this exception.
    if method == "getOpenSubsonicExtensions":
        return reply(values, payload={"openSubsonicExtensions": EXTENSIONS})
    user = authenticate(
        request,
        values,
        require_link=not (
            method in RADIO_STATION_METHODS
            or (
                method == "getCoverArt"
                and values.get("id", "").startswith("station-")
            )
        ),
    )
    if isinstance(user, Response):
        return user
    try:
        result = await dispatch(request, method, values, user)
        record_activity(request, method, values, user)
        return (
            result
            if isinstance(result, Response)
            else reply(values, payload=result)
        )
    except CatalogError as error:
        return reply(values, error.code, error.message)
