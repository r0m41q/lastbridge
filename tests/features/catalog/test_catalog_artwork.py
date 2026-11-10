import asyncio

import httpx
import pytest
from support.catalog import ART, search

from lastbridge.features.catalog.domain.documents import artwork_url, thumbnail


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://lh3.googleusercontent.com/asset=w120-h120-l90-rj",
            "https://lh3.googleusercontent.com/asset=w1080-h1080-p-l90-rj",
        ),
        (
            "https://yt3.googleusercontent.com/asset=w60-h60?key=value",
            "https://yt3.googleusercontent.com/asset"
            "=w1080-h1080-p-l90-rj?key=value",
        ),
        (
            "https://yt3.ggpht.com/asset=s120?key=value",
            "https://yt3.ggpht.com/asset=w1080-h1080-p-l90-rj?key=value",
        ),
        (
            "https://yt3.ggpht.com/asset=w60-h60-l90-rj",
            "https://yt3.ggpht.com/asset=w1080-h1080-p-l90-rj",
        ),
        (
            "https://i.ytimg.com/vi/id/hqdefault.jpg",
            "https://i.ytimg.com/vi/id/hqdefault.jpg",
        ),
        (
            "https://lh3.googleusercontent.com/asset?x=w60-h60",
            "https://lh3.googleusercontent.com/asset?x=w60-h60",
        ),
        ("https://example.com/asset=w60-h60", None),
        ("http://lh3.googleusercontent.com/asset=w60-h60", None),
    ],
)
def test_metrolist_style_server_image_size(url, expected):
    assert artwork_url(url) == expected


def test_thumbnail_chooses_largest_valid_dimensions():
    assert (
        thumbnail(
            {
                "thumbnails": [
                    {"url": ART, "width": 1080, "height": 1080},
                    {
                        "url": "https://lh3.googleusercontent.com/small",
                        "width": 60,
                        "height": 60,
                    },
                    {
                        "url": "https://example.com/large",
                        "width": 8000,
                        "height": 8000,
                    },
                    None,
                ]
            }
        )
        == ART
    )


@pytest.mark.parametrize("status", [404, 302, 503])
def test_larger_artwork_falls_back_without_following_redirects(
    catalog_service, status
):
    _, app, _, _ = catalog_service
    song = search(catalog_service)["song"][0]
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if str(request.url) == artwork_url(ART):
            return httpx.Response(
                status, headers={"location": "http://127.0.0.1/private"}
            )
        assert str(request.url) == ART
        return httpx.Response(
            200,
            content=b"original artwork",
            headers={"content-type": "image/jpeg"},
        )

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as client:
            assert await app.state.services.catalog.artwork(
                client, song["id"]
            ) == (b"original artwork", "image/jpeg")

    asyncio.run(scenario())
    assert calls == [artwork_url(ART), ART]
