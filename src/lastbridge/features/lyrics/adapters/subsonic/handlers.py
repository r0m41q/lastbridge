from lastbridge.transport.http.context import services
from lastbridge.transport.subsonic.parameters import required


async def lyrics(request, method, values, user):
    return {
        "lyricsList": {
            "structuredLyrics": await services(request).lyrics.get(
                required(values, "id"), fetch=request.method != "HEAD"
            )
        }
    }


async def legacy_lyrics(request, method, values, user):
    return {
        "lyrics": {"value": ""}
        | await services(request).lyrics.by_name(
            values.get("artist", ""),
            values.get("title", ""),
            fetch=request.method != "HEAD",
        )
    }


HANDLERS = {
    "getLyricsBySongId": lyrics,
    "getLyrics": legacy_lyrics,
}
