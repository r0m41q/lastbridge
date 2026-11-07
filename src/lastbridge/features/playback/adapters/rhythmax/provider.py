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
    def __init__(self, recording_lookup, timeout=180):
        self.recording_lookup = recording_lookup
        self.timeout = timeout

    async def configure(self, settings):
        self.timeout = settings.timeout_seconds

    async def fetch(self, source, policy, directory, maximum, reserve):
        try:
            recording = self.recording_lookup(source)
        except Exception:
            raise CatalogError(70, "Recording not found") from None
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "lastbridge.features.playback.adapters.rhythmax.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            payload = {
                "recording": {
                    key: recording.get(key)
                    for key in ("artist", "title", "album", "duration")
                },
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
                            0, "Invalid Rhythmax provider response"
                        )
                await process.wait()
                return json.loads(output)

            result = await asyncio.wait_for(exchange(), self.timeout)
            if process.returncode:
                logger.warning(
                    "Rhythmax audio worker exited source=%s exit_code=%s",
                    source,
                    process.returncode,
                )
                raise CatalogError(
                    0, "Rhythmax audio provider unavailable; retry"
                )
            if not isinstance(result, dict):
                logger.warning(
                    "Rhythmax audio worker returned an invalid response "
                    "source=%s",
                    source,
                )
                raise CatalogError(
                    0, "Rhythmax audio provider unavailable; retry"
                )
            if result.get("error"):
                logger.warning(
                    "Rhythmax audio worker reported failure source=%s "
                    "reason=%s status=%s",
                    source,
                    result["error"],
                    result.get("status", "-"),
                )
                messages = {
                    "no_match": "Recording unavailable from Rhythmax",
                    "preview_only": (
                        "Rhythmax only offers a preview for this recording"
                    ),
                    "segment_unavailable": (
                        "Rhythmax audio stream is temporarily unavailable"
                    ),
                    "storage_full": "Not enough free space for audio",
                    "too_large": "Recording exceeds the audio file size limit",
                }
                raise CatalogError(
                    0,
                    messages.get(
                        result["error"],
                        "Rhythmax audio provider unavailable; retry",
                    ),
                )
            return result
        except TimeoutError:
            logger.warning(
                "Rhythmax audio worker timed out source=%s", source
            )
            raise CatalogError(
                0, "Rhythmax audio acquisition timed out; retry"
            ) from None
        except (ValueError, TypeError, OSError) as error:
            logger.warning(
                "Rhythmax audio worker failed source=%s error_type=%s",
                source,
                type(error).__name__,
            )
            raise CatalogError(
                0, "Rhythmax audio provider unavailable; retry"
            ) from None
        finally:
            if process is not None:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                await process.communicate()
