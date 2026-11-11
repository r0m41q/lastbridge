import io

from yt_dlp.networking.common import Response
from yt_dlp.networking.exceptions import HTTPError

from lastbridge.shared.errors import CatalogError
from support.build_audio import make_audio

SOURCE = "khnokW3Mw24"


OTHER = "HYXGY67fATk"


THIRD = "d8CbKd69KT8"


AUDIO = bytes(range(256)) * 32


def youtube_http_error(
    status=403,
    url="https://rr1.googlevideo.com/videoplayback?token=secret",
    retry="120",
):
    return HTTPError(
        Response(
            io.BytesIO(b"secret response body"),
            url,
            {"Retry-After": retry, "Set-Cookie": "secret"},
            status=status,
        )
    )


class FakeAudio:
    def __init__(self, data=AUDIO):
        self.data = data
        self.calls = []
        self.fail = False

    async def fetch(self, source, policy, directory, maximum, reserve):
        self.calls.append(source)
        extension = "m4a" if policy == "aac" else "webm"
        path = directory / f"audio.{extension}"
        path.write_bytes(self.data)
        if self.fail:
            raise CatalogError(0, "Audio provider unavailable; retry")
        return {"policy": policy}


def cache_at(tmp_path, provider=None, **kwargs):
    return make_audio(
        tmp_path / "audio",
        budget=32_768,
        maximum=8192,
        reserve=0,
        provider=provider or FakeAudio(),
        **kwargs,
    )
