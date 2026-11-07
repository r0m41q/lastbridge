import asyncio

import httpx

from lastbridge.features.playback.adapters.rhythmax.client import (
    RhythmaxClient,
)
from lastbridge.features.playback.adapters.rhythmax.worker import (
    choose_candidate,
    parse_manifest,
)


def test_rhythmax_candidate_matches_recording_metadata():
    assert choose_candidate(
        [
            {
                "id": 123,
                "title": "Instant Crush",
                "artist": {"name": "Daft Punk"},
                "album": {"title": "Random Access Memories"},
                "duration": 337,
                "allowStreaming": True,
            },
            {
                "id": 124,
                "title": "Instant Crush",
                "artist": {"name": "Daft Punk"},
                "album": {"title": "Different release"},
                "duration": 337,
                "allowStreaming": True,
            },
        ],
        {
            "artist": "Daft Punk",
            "title": "Instant Crush",
            "album": "Random Access Memories",
            "duration": 337,
        },
    ) == "123"


def test_rhythmax_dash_manifest_expands_numbered_segments():
    codec, urls = parse_manifest(
        "https://audio.example/track/manifest.mpd",
        """<?xml version='1.0'?>
        <MPD>
          <Period>
            <AdaptationSet mimeType='audio/mp4'>
              <Representation id='audio' codecs='flac'>
                <SegmentTemplate initialization='init-$RepresentationID$.m4s'
                    media='seg-$Number$.m4s' startNumber='1'>
                  <SegmentTimeline>
                    <S d='10' r='2'/>
                  </SegmentTimeline>
                </SegmentTemplate>
              </Representation>
            </AdaptationSet>
          </Period>
        </MPD>""",
    )

    assert codec == "flac"
    assert urls == [
        "https://audio.example/track/init-audio.m4s",
        "https://audio.example/track/seg-1.m4s",
        "https://audio.example/track/seg-2.m4s",
        "https://audio.example/track/seg-3.m4s",
    ]


def test_rhythmax_client_uses_public_tokens_and_manifest_api():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "auth.odskyler.workers.dev":
            return httpx.Response(200, json={"access_token": "tidal-token"})
        if (
            request.url.host == "hifi.odskyler.com"
            and request.url.path == "/token"
        ):
            return httpx.Response(200, json={"access_token": "hifi-token"})
        if request.url.host == "api.tidal.com":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": 123,
                            "title": "Instant Crush",
                            "artist": {"name": "Daft Punk"},
                            "album": {"title": "Random Access Memories"},
                            "duration": 337,
                        }
                    ]
                },
            )
        if (
            request.url.host == "hifi.odskyler.com"
            and request.url.path == "/manifests"
        ):
            return httpx.Response(
                200,
                json={"data": {"attributes": {"uri": "https://example"}}},
            )
        return httpx.Response(404)

    async def scenario():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = RhythmaxClient(http)
            search = await client.search("Daft Punk Instant Crush")
            manifest = await client.manifest("123")
            assert search["items"][0]["id"] == 123
            assert manifest["data"]["attributes"]["uri"] == "https://example"

    asyncio.run(scenario())
    assert any(
        request.headers.get("Authorization") == "Bearer tidal-token"
        for request in calls
        if request.url.host == "api.tidal.com"
    )
    assert any(
        request.headers.get("Authorization") == "Bearer hifi-token"
        for request in calls
        if request.url.host == "hifi.odskyler.com"
    )
