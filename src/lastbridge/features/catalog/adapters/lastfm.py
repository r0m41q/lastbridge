import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

from lastbridge.shared.errors import CatalogError
from lastbridge.shared.lastfm import LastfmRequestError
from lastbridge.shared.recordings import normalize


class Biography(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("p", "br") and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def profile_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme in ("http", "https")
            and url.hostname in ("www.last.fm", "last.fm")
            and url.port in (None, 80, 443)
            and not url.username
            and not url.password
            and url.path.startswith("/music/")
        ):
            return urlunsplit(("https", "www.last.fm", url.path, "", ""))
    except ValueError:
        pass
    return None


class LastfmArtistMetadata:
    def __init__(self, lastfm, client):
        self.lastfm, self.client = lastfm, client

    @property
    def configured(self):
        # Public methods need only the application API key, never a linked
        # user's session or the application signing secret.
        return bool(self.lastfm.api_key and self.client)

    async def top_tracks(self, name):
        try:
            rows = await self.lastfm.taste_rows(
                self.client,
                "artist.getTopTracks",
                artist=name,
                autocorrect="0",
            )
            return [(a, t) for a, t in rows if normalize(a) == normalize(name)]
        except LastfmRequestError:
            raise CatalogError() from None

    async def info(self, name):
        try:
            row = await self.lastfm.artist_info(self.client, name)
            if not isinstance(row.get("name"), str) or normalize(
                row["name"]
            ) != normalize(name):
                raise CatalogError()
            info = {}
            bio = row.get("bio") or {}
            if not isinstance(bio, dict):
                raise CatalogError()
            text = bio.get("content") or bio.get("summary")
            if isinstance(text, str) and text.strip():
                parser = Biography()
                parser.feed(text[:32000])
                # Clients may render biographies as HTML. Keep readable text,
                # including Last.fm attribution, without executable markup.
                info["biography"] = html.escape(
                    "".join(parser.parts).strip()[:8000]
                )
            mbid = row.get("mbid")
            if isinstance(mbid, str) and re.fullmatch(
                r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", mbid
            ):
                info["musicBrainzId"] = mbid
            if url := profile_url(row.get("url")):
                info["lastFmUrl"] = url
            similar = row.get("similar") or {}
            if not isinstance(similar, dict):
                raise CatalogError()
            artists = similar.get("artist", [])
            if isinstance(artists, dict):
                artists = [artists]
            if not isinstance(artists, list):
                raise CatalogError()
            info["similar"] = [
                a["name"].strip()
                for a in artists[:50]
                if isinstance(a, dict)
                and isinstance(a.get("name"), str)
                and 0 < len(a["name"].strip()) <= 256
            ]
            return info
        except LastfmRequestError:
            raise CatalogError() from None
