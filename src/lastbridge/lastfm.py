import asyncio
import hashlib
import json
import re
from urllib.parse import urlencode

import httpx

# Provider tokens/session keys are opaque. Live web tokens can contain non-hex
# characters despite older API documentation. Bound printable ASCII input;
# the provider validates authenticity during the signed exchange.
AUTH_VALUE = re.compile(r"[\x21-\x7e]{1,2048}\Z")


class LastfmError(Exception):
    pass


class LastfmRequestError(LastfmError):
    def __init__(self, state, message, code=None):
        self.state, self.code = state, code
        super().__init__(message)


class Lastfm:
    def __init__(self, api_key: str = "", api_secret: str = "") -> None:
        self.api_key, self.api_secret = api_key, api_secret

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def authorization_url(self, callback: str) -> str:
        return "https://www.last.fm/api/auth/?" + urlencode(
            {"api_key": self.api_key, "cb": callback}
        )

    def signed(self, parameters):
        parameters = parameters | {"api_key": self.api_key}
        message = (
            "".join(key + value for key, value in sorted(parameters.items()))
            + self.api_secret
        )
        return parameters | {
            "api_sig": hashlib.md5(
                message.encode(), usedforsecurity=False
            ).hexdigest(),
            "format": "json",
        }

    async def request(self, client, parameters, write=False):
        # Never retain response/request URLs, provider messages or secrets in
        # exceptions. Bound even malformed/decompressed provider responses.
        try:
            options = {"data" if write else "params": parameters}
            async with (
                asyncio.timeout(30),
                client.stream(
                    "POST" if write else "GET",
                    "https://ws.audioscrobbler.com/2.0/",
                    follow_redirects=False,
                    **options,
                ) as response,
            ):
                data = bytearray()
                async for chunk in response.aiter_bytes(65536):
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024:
                        raise ValueError
                values = json.loads(data)
                if not isinstance(values, dict):
                    raise ValueError
                if "error" in values:
                    code = int(values["error"])
                    if not 1 <= code <= 999:
                        raise ValueError
                    state = (
                        "blocked"
                        if code == 9
                        else "pending"
                        if code in (11, 16, 29)
                        else "failed"
                    )
                    raise LastfmRequestError(
                        state, f"Last.fm returned error {code}.", code
                    )
                if response.status_code != 200:
                    raise ValueError
                return values
        except httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout:
            raise LastfmRequestError(
                "pending", "Last.fm could not be reached; retry scheduled."
            ) from None
        except httpx.HTTPError, ValueError, TypeError, TimeoutError:
            raise LastfmRequestError(
                "uncertain" if write else "pending",
                "Last.fm delivery is uncertain; review before retrying."
                if write
                else "Last.fm could not be read; retry scheduled.",
            ) from None

    async def scrobble(self, client, secret, report):
        parameters = {
            "method": "track.scrobble",
            "sk": secret,
            "artist": report["artist"],
            "track": report["title"],
            "timestamp": str(report["timestamp"]),
            "duration": str(report["duration"]),
        }
        if report["album"]:
            parameters["album"] = report["album"]
        values = await self.request(
            client, self.signed(parameters), write=True
        )
        try:
            result = values["scrobbles"]
            entry = result["scrobble"]
            if isinstance(entry, list):
                if len(entry) != 1:
                    raise ValueError
                entry = entry[0]
            code = int(entry["ignoredMessage"]["code"])
            accepted = int(result["@attr"]["accepted"])
            ignored = int(result["@attr"]["ignored"])
            if accepted == 1 and ignored == 0 and code == 0:
                return "sent", None
            if accepted == 0 and ignored == 1 and code > 0:
                return "ignored", f"Last.fm ignored this report (code {code})."
            raise ValueError
        except KeyError, TypeError, ValueError:
            raise LastfmRequestError(
                "uncertain",
                "Last.fm delivery is uncertain; review before retrying.",
            ) from None

    async def loved(self, client, name, page):
        values = await self.request(
            client,
            {
                "method": "user.getLovedTracks",
                "user": name,
                "page": str(page),
                "limit": "50",
                "api_key": self.api_key,
                "format": "json",
            },
        )
        try:
            result = values["lovedtracks"]
            if not isinstance(result, dict):
                raise ValueError
            total = int(result["@attr"]["totalPages"])
            actual = int(result["@attr"]["page"])
            tracks = result.get("track", [])
            if isinstance(tracks, dict):
                tracks = [tracks]
            if (
                actual != page
                or not 0 <= total <= 100000
                or not isinstance(tracks, list)
                or len(tracks) > 50
                or (total == 0 and tracks)
            ):
                raise ValueError
            rows = []
            for track in tracks:
                artist, title = track["artist"]["name"], track["name"]
                if not all(
                    isinstance(v, str) and 0 < len(v) <= 1024
                    for v in (artist, title)
                ):
                    raise ValueError
                rows.append((artist, title))
            return rows, total
        except KeyError, TypeError, ValueError:
            raise LastfmRequestError(
                "pending", "Invalid Last.fm loved-track page; retry scheduled."
            ) from None

    async def taste_rows(self, client, method, **parameters):
        """Bounded first-page taste data; no session key is needed."""
        roots = {
            "user.getTopTracks": ("toptracks", "track"),
            "user.getRecentTracks": ("recenttracks", "track"),
            "user.getTopArtists": ("topartists", "artist"),
            "track.getSimilar": ("similartracks", "track"),
            "artist.getTopTracks": ("toptracks", "track"),
            "chart.getTopTracks": ("tracks", "track"),
        }
        root, kind = roots[method]
        values = await self.request(
            client,
            parameters
            | {
                "method": method,
                "api_key": self.api_key,
                "format": "json",
                "limit": "50",
                "page": "1",
            },
        )
        try:
            rows = values[root].get(kind, [])
            if isinstance(rows, dict):
                rows = [rows]
            # Recent tracks may include a now-playing row in addition to limit.
            if not isinstance(rows, list) or len(rows) > 51:
                raise ValueError
            result = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError
                if row.get("@attr", {}).get("nowplaying") == "true":
                    continue
                title = row["name"]
                if kind == "artist":
                    artist = title
                else:
                    credit = row["artist"]
                    artist = credit.get("name", credit.get("#text"))
                if not all(
                    isinstance(v, str) and 0 < len(v.strip()) <= 1024
                    for v in (artist, title)
                ):
                    raise ValueError
                result.append((artist.strip(), title.strip()))
            return result[:50]
        except KeyError, TypeError, ValueError, AttributeError:
            raise LastfmRequestError(
                "pending", "Invalid Last.fm taste data; retry scheduled."
            ) from None

    async def exchange(
        self, client: httpx.AsyncClient, token: str
    ) -> tuple[str, str]:
        parameters = {
            "api_key": self.api_key,
            "method": "auth.getSession",
            "token": token,
        }
        message = (
            "".join(key + value for key, value in sorted(parameters.items()))
            + self.api_secret
        )
        parameters["api_sig"] = hashlib.md5(
            message.encode(), usedforsecurity=False
        ).hexdigest()
        try:
            response = await client.get(
                "https://ws.audioscrobbler.com/2.0/",
                params=parameters | {"format": "json"},
            )
            response.raise_for_status()
            values = response.json()
            name = values["session"]["name"]
            key = values["session"]["key"]
            if (
                not isinstance(name, str)
                or not 1 <= len(name) <= 255
                or not isinstance(key, str)
                or not AUTH_VALUE.fullmatch(key)
            ):
                raise ValueError
        except httpx.HTTPError, ValueError, KeyError, TypeError:
            raise LastfmError(
                "Last.fm linking failed; start a new link."
            ) from None
        return name, key
