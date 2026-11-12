from lastbridge.shared.errors import StorageError
from lastbridge.transport.http.context import services
from lastbridge.transport.subsonic.parameters import required


async def lyrics(request, method, values, user):
    recording = services(request).library.recording(
        user["id"], required(values, "id"), "song"
    )
    return {
        "lyricsList": {
            "structuredLyrics": await services(request).lyrics.get(
                recording["recording_id"], fetch=request.method != "HEAD"
            )
        }
    }


async def legacy_lyrics(request, method, values, user):
    lyrics = services(request).lyrics
    artist, title = values.get("artist", ""), values.get("title", "")
    result = {}
    if request.method != "HEAD" and all(
        value.strip() and len(value) <= 1024 for value in (artist, title)
    ):
        try:
            song_id = services(request).catalog.song_named(artist, title)
            if song_id is not None:
                song = services(request).library.recording(
                    user["id"], song_id, "song"
                )
                result = await lyrics.plain(song["recording_id"])
        except StorageError:
            pass
    return {"lyrics": {"value": ""} | result}


HANDLERS = {
    "getLyricsBySongId": lyrics,
    "getLyrics": legacy_lyrics,
}
