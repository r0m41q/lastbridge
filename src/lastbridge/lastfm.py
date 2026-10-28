import hashlib
import re
from urllib.parse import urlencode

import httpx

# Provider tokens/session keys are opaque. Live web tokens can contain non-hex
# characters despite older API documentation. Bound printable ASCII input;
# the provider validates authenticity during the signed exchange.
AUTH_VALUE = re.compile(r"[\x21-\x7e]{1,2048}\Z")


class LastfmError(Exception):
    pass


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
