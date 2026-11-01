import functools
import shutil
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from lastbridge.providers.youtube_worker import sample_audio


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Optional probe media tools are not installed",
)
def test_real_sample_remux_probe_and_decode(tmp_path: Path) -> None:
    source = tmp_path / "source.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "12",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

    handler = functools.partial(QuietHandler, directory=str(tmp_path))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = sample_audio(
                {
                    "ext": "m4a",
                    "url": f"http://127.0.0.1:{server.server_port}/source.m4a",
                    "http_headers": {"User-Agent": "Lastbridge test"},
                },
                "abcdefghijk",
                tmp_path / "sample",
            )
        finally:
            server.shutdown()
            thread.join(timeout=3)
    assert result["decode_ok"]
    stream = result["ffprobe"]["streams"][0]
    assert stream["codec_name"] == "aac"
    assert stream["sample_rate"] == "44100"
    assert 9.9 <= float(result["ffprobe"]["format"]["duration"]) <= 10.1
    assert (tmp_path / "sample" / result["file"]).stat().st_size > 0
