import asyncio
import contextlib
import json
import os
import signal
import sys

from lastbridge.shared.errors import CatalogError


class AudioProvider:
    def __init__(self, timeout=180):
        self.timeout = timeout

    async def fetch(self, source, policy, directory, maximum, reserve):
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "lastbridge.features.playback.adapters.youtube.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            payload = {
                "source": source,
                "policy": policy,
                "directory": str(directory),
                "maximum": maximum,
                "reserve": reserve,
            }

            async def exchange():
                process.stdin.write(json.dumps(payload).encode())
                await process.stdin.drain()
                process.stdin.close()
                output = bytearray()
                while chunk := await process.stdout.read(4096):
                    output.extend(chunk)
                    if len(output) > 64 * 1024:
                        raise CatalogError(
                            0, "Invalid audio provider response"
                        )
                await process.wait()
                return json.loads(output)

            result = await asyncio.wait_for(exchange(), self.timeout)
            if process.returncode or not isinstance(result, dict):
                raise CatalogError(0, "Audio provider unavailable; retry")
            if result.get("error"):
                messages = {
                    "access_denied": "Audio provider denied access; try later",
                    "unavailable": "Recording unavailable from audio provider",
                    "no_audio_format": "Requested source audio unavailable",
                    "storage_full": "Not enough free space for audio",
                    "too_large": "Recording exceeds the audio file size limit",
                }
                raise CatalogError(
                    0,
                    messages.get(
                        result["error"], "Audio provider unavailable; retry"
                    ),
                )
            return result
        except TimeoutError:
            raise CatalogError(
                0, "Audio acquisition timed out; retry"
            ) from None
        except ValueError, TypeError, OSError:
            raise CatalogError(
                0, "Audio provider unavailable; retry"
            ) from None
        finally:
            if process is not None:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                await process.communicate()
