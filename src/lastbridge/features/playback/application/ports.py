from asyncio import Lock
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol


class AudioProvider(Protocol):
    async def fetch(
        self,
        source: str,
        policy: str,
        directory: Path,
        maximum: int,
        reserve: int,
    ) -> dict: ...


class AudioStorage(Protocol):
    directory: Path
    incoming: Path
    budget: int
    maximum: int
    reserve: int
    policy: str
    allowance: int
    reserved: int
    closed: bool
    entries: dict
    jobs: dict
    leases: Counter[str]
    pins: set
    lock: Lock

    def path(self, key: str, entry: dict) -> Path: ...
    def manifest(self, key: str, entry: dict): ...
    def relocate(self, key: str, saved: bool): ...
    def trim(self, allowance: int): ...
    def cleanup_aliases(self, only: str | None = None): ...
    def cached_entry(self, key: str) -> dict | None: ...
    def check_space(self): ...
    def staging(self) -> AbstractContextManager[Path]: ...
    def validate(self, directory: Path, result: dict) -> dict: ...
    def destination(self, key: str) -> Path: ...
    def publish(self, key: str, directory: Path, entry: dict): ...
    def discard(self, destination: Path): ...
    def close(self): ...


ResponseFactory = Callable[..., object]
