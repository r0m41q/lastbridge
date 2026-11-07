import asyncio
import fcntl
import json
import math
import os
import shutil
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from lastbridge.features.playback.domain.models import KEY, RENDITIONS
from lastbridge.shared.errors import CatalogError


class AudioFiles:
    """Own committed manifests, file placement and active cache state."""

    def __init__(
        self,
        directory: Path,
        budget=1_000_000_000,
        maximum=64 * 1024 * 1024,
        reserve=256 * 1024 * 1024,
        policy="aac",
        saved_directory=None,
        pins=(),
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
        self.lock = asyncio.Lock()
        self.entries = {}
        self.jobs = {}
        self.leases = Counter()
        self.reserved = 0
        self.closed = False
        self.pins = set(pins)
        self.saved_directory = saved_directory
        if saved_directory is not None:
            saved_directory.mkdir(parents=True, exist_ok=True)
        self.incoming = directory / ".incoming"
        try:
            # Only our private staging tree is disposable. Never touch saved/.
            shutil.rmtree(self.incoming, ignore_errors=True)
            self.incoming.mkdir()
            roots = [(directory, False)]
            if saved_directory is not None:
                roots.append((saved_directory, True))
            for root, saved in roots:
                self.restore(root, saved)
            if self.saved_directory is not None:
                for key in self.pins & self.entries.keys():
                    self.relocate(key, True)
            self.cleanup_aliases()
            self.trim(0)
        except BaseException:
            self.owner.close()
            raise

    def path(self, key, entry):
        root = self.saved_directory if entry.get("saved") else self.directory
        return root / f"{key}.{RENDITIONS[entry['policy']][0]}"

    def restore(self, directory, saved):
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
                    or not 0 < entry["size"] == size
                    or (not saved and size > self.maximum)
                    or not isinstance(entry["used"], (int, float))
                    or not math.isfinite(entry["used"])
                ):
                    raise ValueError
                entry["saved"] = saved
                self.entries[key] = entry
            except FileNotFoundError:
                # Intent remains in SQLite and can reacquire missing bytes.
                path.unlink(missing_ok=True)
            except OSError, ValueError, TypeError, KeyError:
                if saved:
                    # Never silently delete damaged permanently saved music.
                    raise RuntimeError(
                        "Saved audio needs repair from backup"
                    ) from None
                path.unlink(missing_ok=True)

    def cleanup_aliases(self, only=None):
        # Promotion/demotion uses committed copies so a response constructed
        # before the move still has its path until its last lease ends. On
        # restart no leases survive; manifests choose the authoritative copy.
        roots = [self.directory]
        if self.saved_directory is not None:
            roots.append(self.saved_directory)
        for root in roots:
            for extension in ("m4a", "webm"):
                paths = (
                    [root / f"{only}.{extension}"]
                    if only
                    else root.glob(f"*.{extension}")
                )
                for path in paths:
                    if not path.exists():
                        continue
                    key = path.stem
                    if not KEY.fullmatch(key) or (only and key != only):
                        continue
                    entry = self.entries.get(key)
                    if not self.leases[key] and (
                        not entry or path != self.path(key, entry)
                    ):
                        (root / f"{key}.json").unlink(missing_ok=True)
                        path.unlink(missing_ok=True)

    def trim(self, allowance):
        candidates = sorted(
            self.entries, key=lambda k: self.entries[k]["used"]
        )
        while (
            sum(e["size"] for e in self.entries.values() if not e.get("saved"))
            + self.reserved
            + allowance
            > self.budget
        ):
            key = next(
                (
                    k
                    for k in candidates
                    if not self.leases[k]
                    and k not in self.pins
                    and not self.entries[k].get("saved")
                ),
                None,
            )
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
        root = self.saved_directory if entry.get("saved") else self.directory
        temporary = root / f".{key}.json.tmp"
        with temporary.open("w") as output:
            json.dump(entry, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(root / f"{key}.json")

    def _copy_atomic(self, source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            try:
                with source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
                os.replace(temporary_path, destination)
            except BaseException:
                temporary_path.unlink(missing_ok=True)
                raise

    def relocate(self, key, saved):
        entry = self.entries[key]
        target = dict(entry, saved=saved)
        source, destination = self.path(key, entry), self.path(key, target)
        if source == destination:
            return
        if saved and self.saved_directory is not None:
            if shutil.disk_usage(self.saved_directory).free < (
                self.reserve + source.stat().st_size
            ):
                raise CatalogError(0, "Not enough free space for audio")
        # Cache and saved audio may be on different filesystems. Copy through
        # a same-directory temporary file, then commit the destination before
        # removing the old copy. This leaves at most a short-lived duplicate.
        self._copy_atomic(source, destination)
        self.manifest(key, target)
        self.entries[key] = target
        # Delete the old commit marker before deleting the old data path.
        source.with_suffix(".json").unlink(missing_ok=True)
        source.unlink(missing_ok=True)
        self.cleanup_aliases(key)

    def close(self):
        self.owner.close()

    def cached_entry(self, key):
        entry = self.entries.get(key)
        if entry:
            path = self.path(key, entry)
            if not path.is_file() or path.stat().st_size != entry["size"]:
                if entry.get("saved") and path.exists():
                    raise CatalogError(
                        0, "Saved audio needs repair from backup"
                    )
                del self.entries[key]
                path.unlink(missing_ok=True)
                path.with_suffix(".json").unlink(missing_ok=True)
                entry = None
        return entry

    def check_space(self):
        if shutil.disk_usage(self.directory).free < (
            self.reserve + self.reserved + self.allowance
        ):
            raise CatalogError(0, "Not enough free space for audio")

    @contextmanager
    def staging(self):
        with tempfile.TemporaryDirectory(dir=self.incoming) as directory:
            yield Path(directory)

    def validate(self, directory, result):
        extension = RENDITIONS[self.policy][0]
        path = directory / f"audio.{extension}"
        size = path.stat().st_size
        if not 0 < size <= self.maximum or result.get("policy") != self.policy:
            raise CatalogError(0, "Invalid audio provider result")
        with path.open("rb") as completed:
            os.fsync(completed.fileno())
        return {"policy": self.policy, "size": size, "used": time.time()}

    def destination(self, key, saved=False):
        root = (
            self.saved_directory
            if saved and self.saved_directory
            else self.directory
        )
        return root / f"{key}.{RENDITIONS[self.policy][0]}"

    def publish(self, key, directory, entry):
        extension = RENDITIONS[self.policy][0]
        source = directory / f"audio.{extension}"
        destination = self.destination(key, entry.get("saved", False))
        if source.stat().st_dev == destination.parent.stat().st_dev:
            source.replace(destination)
        else:
            self._copy_atomic(source, destination)
        self.manifest(key, entry)
        self.entries[key] = entry

    def discard(self, destination):
        destination.unlink(missing_ok=True)
