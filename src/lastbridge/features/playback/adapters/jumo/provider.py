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
    def __init__(self, recording_lookup, timeout=180, settings=None):
        self.recording_lookup = recording_lookup
        self.timeout = timeout
        self.settings = settings

    async def configure(self, settings):
        self.settings = settings
        self.timeout = settings.timeout_seconds

    async def fetch(self, source, policy, directory, maximum, reserve):
        if self.settings is None:
            raise CatalogError(0, "Jumo audio provider unavailable; retry")
        try:
            recording = self.recording_lookup(source)
        except Exception:
            raise CatalogError(70, "Recording not found") from None
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "lastbridge.features.playback.adapters.jumo.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            payload = {
                "base_url": self.settings.jumo_base_url,
                "region": self.settings.jumo_region,
                "quality": self.settings.jumo_quality,
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
                        raise CatalogError(0, "Invalid Jumo provider response")
                await process.wait()
                return json.loads(output)

            result = await asyncio.wait_for(exchange(), self.timeout)
            if process.returncode:
                logger.warning(
                    "Jumo audio worker exited source=%s exit_code=%s",
                    source,
                    process.returncode,
                )
                raise CatalogError(0, "Jumo audio provider unavailable; retry")
            if not isinstance(result, dict):
                logger.warning(
                    "Jumo audio worker returned an invalid response source=%s",
                    source,
                )
                raise CatalogError(0, "Jumo audio provider unavailable; retry")
            if result.get("error"):
                logger.warning(
                    "Jumo audio worker reported failure source=%s reason=%s",
                    source,
                    result["error"],
                )
                messages = {
                    "no_match": "Recording unavailable from Jumo-DL",
                    "preview_only": (
                        "Jumo-DL only offers a preview for this recording"
                    ),
                    "no_audio_format": (
                        "Requested recording quality unavailable from Jumo-DL"
                    ),
                    "storage_full": "Not enough free space for audio",
                    "too_large": "Recording exceeds the audio file size limit",
                }
                raise CatalogError(
                    0,
                    messages.get(
                        result["error"],
                        "Jumo audio provider unavailable; retry",
                    ),
                )
            return result
        except TimeoutError:
            logger.warning("Jumo audio worker timed out source=%s", source)
            raise CatalogError(
                0, "Jumo audio acquisition timed out; retry"
            ) from None
        except (ValueError, TypeError, OSError) as error:
            logger.warning(
                "Jumo audio worker failed source=%s error_type=%s",
                source,
                type(error).__name__,
            )
            raise CatalogError(
                0, "Jumo audio provider unavailable; retry"
            ) from None
        finally:
            if process is not None:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                await process.communicate()
