from lastbridge.transport.subsonic.parameters import required


async def lyrics(request, method, values, user):
    return {
        "lyricsList": {
            "structuredLyrics": await request.app.state.lyrics.get(
                required(values, "id"), fetch=request.method != "HEAD"
            )
        }
    }


HANDLERS = {
    "getLyricsBySongId": lyrics,
}
