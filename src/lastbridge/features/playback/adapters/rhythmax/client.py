import httpx

HIFI_BASE_URL = "https://hifi.odskyler.com"
HIFI_TOKEN_URL = f"{HIFI_BASE_URL}/token"
TIDAL_API_BASE_URL = "https://api.tidal.com/v1"
TIDAL_TOKEN_URL = "https://auth.odskyler.workers.dev/token"
PROXY_HEALTH_URL = "https://proxy.odskyler.workers.dev/health"
SEGMENT_HOST = "sp-ad-fa.audio.tidal.com"


class RhythmaxError(RuntimeError):
    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


class RhythmaxClient:
    def __init__(self, http: httpx.AsyncClient, country: str = "US"):
        self.http = http
        self.country = country
        self.tidal_token = None
        self.hifi_token = None

    async def _token(self, url: str, kind: str) -> str:
        cached = self.tidal_token if kind == "tidal" else self.hifi_token
        if cached:
            return cached
        try:
            response = await self.http.get(url)
        except httpx.HTTPError as error:
            raise RhythmaxError(f"{kind}_token_unavailable") from error
        if response.status_code != 200:
            raise RhythmaxError(
                f"{kind}_token_unavailable", response.status_code
            )
        try:
            token = response.json().get("access_token")
        except ValueError as error:
            raise RhythmaxError(f"{kind}_token_invalid") from error
        if not isinstance(token, str) or not token:
            raise RhythmaxError(f"{kind}_token_invalid")
        if kind == "tidal":
            self.tidal_token = token
        else:
            self.hifi_token = token
        return token

    async def _get_json(
        self, base_url: str, path: str, params: dict[str, str | int], kind: str
    ) -> dict:
        token = await self._token(
            TIDAL_TOKEN_URL if kind == "tidal" else HIFI_TOKEN_URL,
            kind,
        )
        for attempt in range(2):
            try:
                response = await self.http.get(
                    f"{base_url}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as error:
                raise RhythmaxError(f"{kind}_unavailable") from error
            if response.status_code == 401 and attempt == 0:
                if kind == "tidal":
                    self.tidal_token = None
                else:
                    self.hifi_token = None
                token = await self._token(
                    TIDAL_TOKEN_URL if kind == "tidal" else HIFI_TOKEN_URL,
                    kind,
                )
                continue
            if response.status_code != 200:
                raise RhythmaxError(
                    f"{kind}_unavailable", response.status_code
                )
            try:
                payload = response.json()
            except ValueError as error:
                raise RhythmaxError(f"{kind}_invalid_response") from error
            if not isinstance(payload, dict):
                raise RhythmaxError(f"{kind}_invalid_response")
            return payload
        raise RhythmaxError(f"{kind}_unavailable")

    async def search(self, query: str) -> dict:
        return await self._get_json(
            TIDAL_API_BASE_URL,
            "/search/tracks",
            {
                "query": query,
                "limit": 25,
                "offset": 0,
                "countryCode": self.country,
            },
            "tidal",
        )

    async def manifest(self, track_id: str, quality: str = "LOSSLESS") -> dict:
        return await self._get_json(
            HIFI_BASE_URL,
            "/manifests",
            {"id": track_id, "quality": quality},
            "hifi",
        )

    async def proxy_urls(self) -> list[str]:
        try:
            response = await self.http.get(PROXY_HEALTH_URL)
            payload = response.json() if response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return []
        active = payload.get("active", [])
        if not isinstance(active, list):
            return []
        return [
            str(row["url"]).rstrip("/")
            for row in active
            if isinstance(row, dict)
            and row.get("status") == 200
            and isinstance(row.get("url"), str)
        ]

    async def fetch_segment(self, url: str, proxies: list[str]) -> bytes:
        try:
            if httpx.URL(url).host == SEGMENT_HOST:
                for proxy in proxies:
                    try:
                        response = await self.http.get(
                            f"{proxy}/proxy/segment", params={"url": url}
                        )
                    except httpx.HTTPError:
                        continue
                    if response.status_code == 200:
                        return response.content
                raise RhythmaxError("segment_unavailable")
            response = await self.http.get(url)
        except httpx.HTTPError as error:
            raise RhythmaxError("segment_unavailable") from error
        if response.status_code != 200:
            raise RhythmaxError("segment_unavailable", response.status_code)
        return response.content
