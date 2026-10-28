import asyncio
import contextlib
import fcntl
import json
import math
import os
import re
import shutil
import signal
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import anyio
from starlette.responses import FileResponse

from lastbridge.catalog import CatalogError
from lastbridge.providers.youtube import VIDEO_ID

RENDITIONS = {"aac": ("m4a", "audio/mp4"), "opus": ("webm", "audio/webm")}
KEY = re.compile(r"[A-Za-z0-9_-]{11}-(?:aac|opus)\Z")


class AudioProvider:
    def __init__(self, timeout=180):
        self.timeout = timeout

    async def fetch(self, source, policy, directory, maximum, reserve):
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "lastbridge.providers.audio_worker",
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


class AudioResponse(FileResponse):
    def __init__(self, cache, key, entry, download=False):
        extension, media_type = RENDITIONS[entry["policy"]]
        super().__init__(
            cache.directory / f"{key}.{extension}",
            media_type=media_type,
            filename=f"{key}.{extension}",
            content_disposition_type="attachment" if download else "inline",
        )
        self.cache, self.key = cache, key

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # ASGI disconnect/cancellation must release the active-file lease.
            with anyio.CancelScope(shield=True):
                await self.cache.release(self.key)


class AudioCache:
    """Single-process cache. Manifests commit completed files across restarts.

    Each acquisition reserves two file allowances (source and possible remux);
    reservations plus completed audio never exceed the configured byte budget.
    A request holds its lease from acquisition through the last response byte.
    """

    def __init__(
        self,
        directory: Path,
        budget=1_000_000_000,
        maximum=64 * 1024 * 1024,
        reserve=256 * 1024 * 1024,
        policy="aac",
        provider=None,
    ):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.owner = (directory / ".lock").open("a")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner.close()
            raise RuntimeError("Audio storage is already in use") from None
        self.budget, self.maximum = budget, min(maximum, budget // 2)
        self.allowance = 2 * self.maximum
        self.reserve, self.policy = reserve, policy
        self.provider = provider if provider is not None else AudioProvider()
        self.lock = asyncio.Lock()
        self.entries = {}
        self.jobs = {}
        self.leases = Counter()
        self.reserved = 0
        self.closed = False
        self.incoming = directory / ".incoming"
        try:
            # Only our private staging tree is disposable. Never touch saved/.
            shutil.rmtree(self.incoming, ignore_errors=True)
            self.incoming.mkdir()
            for path in directory.glob("*.json"):
                key = path.stem
                if not KEY.fullmatch(key):
                    continue
                try:
                    entry = json.loads(path.read_text())
                    policy = key.rsplit("-", 1)[1]
                    extension = RENDITIONS[policy][0]
                    size = (directory / f"{key}.{extension}").stat().st_size
                    if (
                        entry["policy"] != policy
                        or not 0 < entry["size"] == size <= self.maximum
                        or not isinstance(entry["used"], (int, float))
                        or not math.isfinite(entry["used"])
                    ):
                        raise ValueError
                    self.entries[key] = entry
                except OSError, ValueError, TypeError, KeyError:
                    path.unlink(missing_ok=True)
            # An uncommitted data file is never considered a complete entry.
            for extension in ("m4a", "webm"):
                for path in directory.glob(f"*.{extension}"):
                    if (
                        KEY.fullmatch(path.stem)
                        and path.stem not in self.entries
                    ):
                        path.unlink()
            self.trim(0)
        except BaseException:
            self.owner.close()
            raise

    def trim(self, allowance):
        candidates = sorted(
            self.entries, key=lambda k: self.entries[k]["used"]
        )
        while (
            sum(e["size"] for e in self.entries.values())
            + self.reserved
            + allowance
            > self.budget
        ):
            key = next((k for k in candidates if not self.leases[k]), None)
            if key is None:
                raise CatalogError(0, "Audio cache busy; retry shortly")
            candidates.remove(key)
            entry = self.entries[key]
            extension = RENDITIONS[entry["policy"]][0]
            (self.directory / f"{key}.json").unlink(missing_ok=True)
            (self.directory / f"{key}.{extension}").unlink(missing_ok=True)
            del self.entries[key]

    def manifest(self, key, entry):
        # Called inside the cache lock. A crash leaves old or new complete
        # metadata; staging is discarded on the next startup.
        temporary = self.incoming / f"{key}.json"
        with temporary.open("w") as output:
            json.dump(entry, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(self.directory / f"{key}.json")

    async def acquire(self, source):
        if not VIDEO_ID.fullmatch(source):
            raise CatalogError(70, "Recording not found")
        key = f"{source}-{self.policy}"
        try:
            async with self.lock:
                if self.closed:
                    raise CatalogError(0, "Audio cache is shutting down")
                entry = self.entries.get(key)
                if entry:
                    extension = RENDITIONS[self.policy][0]
                    path = self.directory / f"{key}.{extension}"
                    if (
                        not path.is_file()
                        or path.stat().st_size != entry["size"]
                    ):
                        del self.entries[key]
                        path.unlink(missing_ok=True)
                        (self.directory / f"{key}.json").unlink(
                            missing_ok=True
                        )
                        entry = None
                if not entry and key not in self.jobs:
                    if len(self.jobs) >= 2:
                        raise CatalogError(
                            0, "Audio provider busy; retry shortly"
                        )
                    self.trim(self.allowance)
                    # Reserve against concurrent jobs on the same filesystem.
                    if shutil.disk_usage(self.directory).free < (
                        self.reserve + self.reserved + self.allowance
                    ):
                        raise CatalogError(
                            0, "Not enough free space for audio"
                        )
                    self.reserved += self.allowance
                    task = asyncio.create_task(self.fill(source, key))
                    self.jobs[key] = task
                    # Retrieve errors even when all requesting clients cancel.
                    task.add_done_callback(
                        lambda t: None if t.cancelled() else t.exception()
                    )
                self.leases[key] += 1
                job = self.jobs.get(key)
            try:
                if job:
                    await asyncio.shield(job)
                async with self.lock:
                    entry = self.entries[key]
                    entry["used"] = time.time()
                    self.manifest(key, entry)
                return key, dict(entry)
            except BaseException:
                with anyio.CancelScope(shield=True):
                    await self.release(key)
                raise
        except OSError:
            raise CatalogError(0, "Audio storage unavailable") from None

    async def fill(self, source, key):
        destination = None
        try:
            with tempfile.TemporaryDirectory(dir=self.incoming) as attempt:
                directory = Path(attempt)
                result = await self.provider.fetch(
                    source,
                    self.policy,
                    directory,
                    self.maximum,
                    self.reserve,
                )
                extension = RENDITIONS[self.policy][0]
                path = directory / f"audio.{extension}"
                size = path.stat().st_size
                if (
                    not 0 < size <= self.maximum
                    or result.get("policy") != self.policy
                ):
                    raise CatalogError(0, "Invalid audio provider result")
                # Flush data before publishing the manifest. Readers only see
                # entries after both file and manifest have been committed.
                with path.open("rb") as completed:
                    os.fsync(completed.fileno())
                entry = {
                    "policy": self.policy,
                    "size": size,
                    "used": time.time(),
                }
                async with self.lock:
                    destination = self.directory / f"{key}.{extension}"
                    path.replace(destination)
                    self.manifest(key, entry)
                    self.entries[key] = entry
        except OSError, ValueError, TypeError, KeyError:
            raise CatalogError(0, "Audio acquisition failed; retry") from None
        finally:
            async with self.lock:
                if destination is not None and key not in self.entries:
                    with contextlib.suppress(OSError):
                        destination.unlink(missing_ok=True)
                self.reserved -= self.allowance
                self.jobs.pop(key, None)

    async def response(self, source, download=False):
        key, entry = await self.acquire(source)
        try:
            return AudioResponse(self, key, entry, download)
        except BaseException:
            await self.release(key)
            raise

    async def release(self, key):
        async with self.lock:
            self.leases[key] -= 1
            if not self.leases[key]:
                del self.leases[key]

    async def close(self):
        self.closed = True
        jobs = list(self.jobs.values())
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self.owner.close()
