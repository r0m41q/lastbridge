import asyncio

import httpx
import pytest

from lastbridge.features.catalog.adapters.lastfm import LastfmArtistMetadata
from lastbridge.providers.lastfm.client import Lastfm
from lastbridge.shared.errors import CatalogError


def test_public_artist_reads_use_key_only_and_sanitize_metadata():
    calls = []

    def handle(request):
        params = dict(request.url.params)
        calls.append(params)
        assert request.method == "GET"
        assert params["artist"] == "Artist" and params["autocorrect"] == "0"
        assert not {"sk", "api_sig", "username", "user"} & params.keys()
        if params["method"] == "artist.getTopTracks":
            assert params["page"] == "1" and params["limit"] == "50"
            return httpx.Response(
                200,
                json={
                    "toptracks": {
                        "track": [
                            {"artist": {"name": "Artist"}, "name": "First"},
                            {"artist": {"name": "Artist"}, "name": "Second"},
                            {"artist": {"name": "Other"}, "name": "Wrong"},
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "artist": {
                    "name": "Artist",
                    "mbid": "12345678-1234-1234-1234-123456789abc",
                    "url": (
                        "http://www.last.fm/music/Artist"
                        "?discard=token#fragment"
                    ),
                    "bio": {
                        "content": (
                            "<p>Artist &amp; friends.</p><script>"
                            'alert("secret")</script><img src=x '
                            'onerror="secret"><a href="javascript:secret">'
                            "Read more on Last.fm</a>&lt;script&gt;"
                            "escaped&lt;/script&gt;"
                        )
                    },
                    "similar": {"artist": {"name": "Similar"}},
                    "image": [{"#text": "https://untrusted.test/token"}],
                }
            },
        )

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle)
        ) as client:
            metadata = LastfmArtistMetadata(Lastfm("test-api-key"), client)
            assert metadata.configured
            assert await metadata.top_tracks("Artist") == [
                ("Artist", "First"),
                ("Artist", "Second"),
            ]
            info = await metadata.info("Artist")
            assert info["lastFmUrl"] == "https://www.last.fm/music/Artist"
            assert info["similar"] == ["Similar"]
            assert "Read more on Last.fm" in info["biography"]
            assert "&lt;script&gt;escaped&lt;/script&gt;" in info["biography"]
            assert "<" not in info["biography"]
            assert "secret" not in str(info) and "ImageUrl" not in str(info)

    asyncio.run(scenario())
    assert len(calls) == 2


@pytest.mark.parametrize(
    "response",
    [
        {"artist": []},
        {"artist": {"name": "Wrong artist"}},
        {"artist": {"name": "Artist", "bio": "bad"}},
        {"artist": {"name": "Artist", "similar": {"artist": "bad"}}},
        {"error": 29, "message": "secret provider URL"},
    ],
)
def test_invalid_artist_metadata_maps_to_fixed_public_error(response):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=response)
            )
        ) as client:
            metadata = LastfmArtistMetadata(Lastfm("key"), client)
            with pytest.raises(CatalogError) as error:
                await metadata.info("Artist")
            assert str(error.value) == "Metadata provider unavailable"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://www.last.fm.evil.test/music/Artist",
        "https://user:password@www.last.fm/music/Artist",
        "https://www.last.fm:1234/music/Artist",
    ],
)
def test_artist_profile_links_exclude_untrusted_urls(url):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "artist": {
                            "name": "Artist",
                            "url": url,
                            "mbid": "invalid",
                            "bio": {"summary": "A summary"},
                        }
                    },
                )
            )
        ) as client:
            info = await LastfmArtistMetadata(Lastfm("key"), client).info(
                "Artist"
            )
            assert info == {"biography": "A summary", "similar": []}

    asyncio.run(scenario())
