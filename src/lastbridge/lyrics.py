import asyncio
import hashlib
import json
import math
import re
import sqlite3
import time
import unicodedata
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

DEADLINE = 8
MAXIMUM = 256 * 1024
STAMP = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:\.(\d{1,3}))?\]")
OFFSET = re.compile(r"\[offset:([+-]?\d{1,6})\]", re.I)
TAG = re.compile(r"\[(?:ar|ti|al|by|re|ve|length):[^\]]*\]", re.I)


def text(value):
    return isinstance(value, str) and all(
        c in "\t\n\r"
        or 0x20 <= ord(c) <= 0xD7FF
        or 0xE000 <= ord(c) <= 0xFFFD
        or 0x10000 <= ord(c) <= 0x10FFFF
        for c in value
    )


def normalize(value):
    # Keep version qualifiers, accents and punctuation. No fuzzy substitution.
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def timed_lines(value, duration):
    if not text(value) or len(value) > 64 * 1024:
        return []
    lines, offset, size = [], 0, 0
    for raw in value.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if match := OFFSET.fullmatch(raw):
            offset = int(match[1])
            if abs(offset) > 60_000:
                return []
            continue
        if TAG.fullmatch(raw):
            continue
        stamps, end = [], 0
        while match := STAMP.match(raw, end):
            fraction = int((match[3] or "0").ljust(3, "0"))
            stamps.append((int(match[1]) * 60 + int(match[2])) * 1000)
            stamps[-1] += fraction
            end = match.end()
        if not stamps or len(raw[end:]) > 2048:
            return []
        size += len(stamps) * len(raw[end:])
        if size > 64 * 1024:
            return []
        lines.extend((start, raw[end:].strip()) for start in stamps)
        if len(lines) > 2000:
            return []
    # Bake the LRC offset into timestamps for consistent client handling.
    if any(start - offset > (duration + 2) * 1000 for start, _ in lines):
        return []
    result = sorted(
        dict.fromkeys((max(0, start - offset), line) for start, line in lines),
        key=lambda row: row[0],
    )
    if not any(line for _, line in result):
        return []
    return [{"start": start, "value": line} for start, line in result]


def structured(song, row):
    if not isinstance(row, dict):
        raise ValueError
    for key, field in (
        ("artist", "artistName"),
        ("title", "trackName"),
        ("album", "albumName"),
    ):
        if not text(row.get(field)) or normalize(song[key]) != normalize(
            row[field]
        ):
            return []
    duration = row.get("duration")
    if (
        type(duration) not in (int, float)
        or not song["duration"] - 2 <= duration <= song["duration"] + 2
    ):
        return []
    if row.get("instrumental") is True:
        return []
    lines = timed_lines(row.get("syncedLyrics"), song["duration"])
    synced = bool(lines)
    if not lines:
        plain = row.get("plainLyrics")
        if text(plain) and len(plain) <= 64 * 1024:
            values = plain.splitlines()
            if len(values) <= 2000 and all(len(v) <= 2048 for v in values):
                lines = [{"value": v} for v in values]
    if not lines or not any(line["value"].strip() for line in lines):
        return []
    return [
        {
            "displayArtist": song["artist"],
            "displayTitle": song["title"],
            "lang": "und",
            "offset": 0,
            "synced": synced,
            "line": lines,
        }
    ]


def retry_after(value):
    try:
        seconds = float(value)
    except ValueError, TypeError:
        try:
            seconds = (
                parsedate_to_datetime(value) - datetime.now(UTC)
            ).total_seconds()
        except ValueError, TypeError, OverflowError:
            return 60
    return max(60, seconds) if math.isfinite(seconds) else 60


class Lyrics:
    def __init__(self, accounts, catalog, client):
        self.accounts, self.catalog, self.client = accounts, catalog, client
        self.tasks = {}
        self.lock = asyncio.Lock()
        self.next_request = 0
        self.blocked_until = 0
        self.closed = False

    def cached(self, song_id, fingerprint):
        with self.accounts.connection() as connection:
            row = connection.execute(
                "SELECT * FROM lyrics WHERE song_id=? AND fingerprint=?",
                (song_id, fingerprint),
            ).fetchone()
        return dict(row) if row else None

    def save(self, song_id, fingerprint, document, ttl, updated=None):
        now = time.time()
        with self.accounts.transaction() as connection:
            connection.execute(
                "INSERT INTO lyrics VALUES(?,?,?,?,?) ON CONFLICT(song_id) "
                "DO UPDATE SET fingerprint=excluded.fingerprint, "
                "document=excluded.document, expires=excluded.expires, "
                "updated=excluded.updated",
                (
                    song_id,
                    fingerprint,
                    json.dumps(document, ensure_ascii=False),
                    now + ttl,
                    updated if updated is not None else now,
                ),
            )
            connection.execute(
                "DELETE FROM lyrics WHERE song_id IN (SELECT song_id FROM "
                "lyrics ORDER BY updated DESC, song_id LIMIT -1 OFFSET 1000)"
            )

    async def fetch(self, song_id, fingerprint, song, previous):
        stale = []
        if previous and previous["updated"] >= time.time() - 30 * 86400:
            stale = json.loads(previous["document"])
        try:
            async with asyncio.timeout(DEADLINE):
                async with self.lock:
                    if self.blocked_until > time.monotonic():
                        return stale
                    await asyncio.sleep(
                        max(0, self.next_request - time.monotonic())
                    )
                    try:
                        async with self.client.stream(
                            "GET",
                            "https://lrclib.net/api/get",
                            params={
                                "artist_name": song["artist"],
                                "track_name": song["title"],
                                "album_name": song["album"],
                                "duration": song["duration"],
                            },
                            headers={
                                "User-Agent": "Lastbridge/0.1.0 "
                                "(https://github.com/r0m41q/lastbridge)"
                            },
                            follow_redirects=False,
                        ) as response:
                            if response.status_code in (429, 503):
                                self.blocked_until = time.monotonic() + (
                                    retry_after(
                                        response.headers.get("Retry-After")
                                    )
                                )
                            if response.status_code == 404:
                                result = []
                            else:
                                response.raise_for_status()
                                output = bytearray()
                                async for chunk in response.aiter_bytes():
                                    output.extend(chunk)
                                    if len(output) > MAXIMUM:
                                        raise ValueError
                                result = structured(song, json.loads(output))
                    finally:
                        self.next_request = time.monotonic() + 0.25
            # The fingerprint invalidates this entry if song metadata changes.
            self.save(
                song_id, fingerprint, result, 7 * 86400 if result else 86400
            )
            return result
        except httpx.HTTPError, TimeoutError, ValueError, sqlite3.Error:
            # Briefly cache transient failures to avoid repeated requests.
            try:
                self.save(
                    song_id,
                    fingerprint,
                    stale,
                    60,
                    previous["updated"] if previous and stale else None,
                )
            except sqlite3.Error:
                pass
            return stale

    async def get(self, song_id, *, fetch=True):
        song = self.catalog.read(song_id, "song")["document"]
        if not fetch or self.closed:
            return []
        if any(
            not text(song.get(key)) or not 0 < len(song[key]) <= 1024
            for key in ("artist", "title", "album")
        ) or not (
            type(song.get("duration")) is int and 1 <= song["duration"] <= 3600
        ):
            return []
        signature = {
            k: song[k] for k in ("artist", "title", "album", "duration")
        }
        fingerprint = hashlib.sha256(
            json.dumps(signature, sort_keys=True).encode()
        ).hexdigest()
        try:
            cached = self.cached(song_id, fingerprint)
            if cached and cached["expires"] > time.time():
                return json.loads(cached["document"])
        except sqlite3.Error, ValueError:
            return []
        key = (song_id, fingerprint)
        if key not in self.tasks:
            if len(self.tasks) >= 4:
                return []
            task = asyncio.create_task(
                self.fetch(song_id, fingerprint, signature, cached)
            )
            self.tasks[key] = task
            task.add_done_callback(lambda _: self.tasks.pop(key, None))
        return await asyncio.shield(self.tasks[key])

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
