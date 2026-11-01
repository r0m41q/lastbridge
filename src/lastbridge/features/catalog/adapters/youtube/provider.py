import asyncio
import contextlib
import json
import sys

from lastbridge.shared.errors import CatalogError


class MetadataProvider:
    def __init__(self, timeout=30):
        self.timeout = timeout
        self.slots = asyncio.Semaphore(2)
        # Artist pages fan out into album requests. Reserve independent bounded
        # capacity for the foreground radio action so those cannot starve it.
        self.radio_slots = asyncio.Semaphore(2)
        self.playlist_slots = asyncio.Semaphore(1)

    async def exchange(self, process, payload):
        process.stdin.write(json.dumps(payload).encode())
        await process.stdin.drain()
        process.stdin.close()
        output = bytearray()
        while chunk := await process.stdout.read(64 * 1024):
            output.extend(chunk)
            if len(output) > 8 * 1024 * 1024:
                raise CatalogError(0, "Metadata response exceeds size limit")
        await process.wait()
        return output

    async def fetch(self, payload):
        slots = (
            self.radio_slots
            if payload.get("operation") == "radio"
            else self.slots
        )
        if payload.get("operation") == "playlist":
            slots = self.playlist_slots
        try:
            await asyncio.wait_for(slots.acquire(), timeout=1)
        except TimeoutError:
            raise CatalogError(
                0, "Metadata provider busy; retry shortly"
            ) from None
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "lastbridge.features.catalog.adapters.youtube.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output = await asyncio.wait_for(
                self.exchange(process, payload), timeout=self.timeout
            )
            result = json.loads(output)
            if (
                process.returncode
                or not isinstance(result, dict)
                or "error" in result
                or not isinstance(result.get("data"), dict)
            ):
                raise CatalogError()
            return result["data"]
        except TimeoutError, ValueError, KeyError, OSError:
            raise CatalogError() from None
        finally:
            if process is not None:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                # Drain the killed worker's pipes; wait() alone can deadlock
                # when unread stdout has paused the subprocess transport.
                await process.communicate()
            slots.release()
