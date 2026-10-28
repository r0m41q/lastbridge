import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlencode

import httpx

HEX_KEY = re.compile(r"[a-fA-F0-9]{32}\Z")


class LastfmError(Exception):
    pass


class Lastfm:
    def __init__(self, path: Path | None) -> None:
        self.api_key = self.api_secret = ""
        if path is not None:
            try:
                values = json.loads(path.read_text())
                key, secret = values["api_key"], values["api_secret"]
                if not HEX_KEY.fullmatch(key) or not HEX_KEY.fullmatch(secret):
                    raise ValueError
            except OSError, ValueError, KeyError, TypeError:
                raise RuntimeError("Invalid Last.fm credential file") from None
            self.api_key, self.api_secret = key, secret

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def authorization_url(self, callback: str) -> str:
        return "https://www.last.fm/api/auth/?" + urlencode(
            {"api_key": self.api_key, "cb": callback}
        )

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
                or not HEX_KEY.fullmatch(key)
            ):
                raise ValueError
        except httpx.HTTPError, ValueError, KeyError, TypeError:
            raise LastfmError(
                "Last.fm linking failed; start a new link."
            ) from None
        return name, key
