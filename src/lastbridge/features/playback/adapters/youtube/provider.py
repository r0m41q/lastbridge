import asyncio
import contextlib
import json
import logging
import os
import signal
import sys

from lastbridge.shared.errors import CatalogError

logger = logging.getLogger(__name__)


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
            if process.returncode:
                logger.warning(
                    "YouTube audio worker exited source=%s exit_code=%s",
                    source,
                    process.returncode,
                )
                raise CatalogError(0, "Audio provider unavailable; retry")
            if not isinstance(result, dict):
                logger.warning(
                    "YouTube audio worker returned an invalid response "
                    "source=%s",
                    source,
                )
                raise CatalogError(0, "Audio provider unavailable; retry")
            if result.get("error"):
                logger.warning(
                    "YouTube audio worker reported failure source=%s "
                    "reason=%s",
                    source,
                    result["error"],
                )
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
            logger.warning("YouTube audio worker timed out source=%s", source)
            raise CatalogError(
                0, "Audio acquisition timed out; retry"
            ) from None
        except (ValueError, TypeError, OSError) as error:
            logger.warning(
                "YouTube audio worker failed source=%s error_type=%s",
                source,
                type(error).__name__,
            )
            raise CatalogError(
                0, "Audio provider unavailable; retry"
            ) from None
        finally:
            if process is not None:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                await process.communicate()
