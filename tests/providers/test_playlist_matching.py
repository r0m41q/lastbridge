import asyncio
import json
from pathlib import Path

import pytest
from support.catalog import SONG
from support.imports import URL, browser, user
from support.library import run

from lastbridge.features.playlists.adapters.youtube.matching import (
    PlaylistResolver,
)
from lastbridge.features.playlists.domain.imports import Snapshot
from lastbridge.features.playlists.domain.matching import compare, identity

CORPUS = json.loads(
    (Path(__file__).parents[1] / "support/playlist_matches.json").read_text()
)


@pytest.mark.parametrize("case", CORPUS, ids=lambda c: str(c["number"]))
def test_live_corpus_never_automatically_accepts_fuzzy_or_mismatch(case):
    async def fetch(payload):
        assert payload["kinds"] == ["songs"]
        return {"songs": case["candidates"]}

    def store(row):
        return {"id": row["videoId"]}

    result = asyncio.run(
        PlaylistResolver(fetch, store).resolve(case["source"])
    )
    assert (result["song"]["id"] if result["song"] else None) == (
        case["expected_song_id"]
    )
    if result["song"]:
        row = next(
            c
            for c in case["candidates"]
            if c["videoId"] == result["song"]["id"]
        )
        assert all(compare(case["source"], row).values())
    if case["number"] == 70:
        assert result["song"] is None
        assert all(c["title"] != "Pound Town 2" for c in result["candidates"])


def test_explicitness_remixer_artist_spacing_and_soundtrack():
    source = SONG | {
        "title": "Get Low [Clean]",
        "artists": [{"name": "East Side"}],
    }
    clean = SONG | {
        "title": "Get Low",
        "isExplicit": False,
        "artists": [{"name": "Eastside"}],
    }
    assert all(compare(source, clean).values())
    assert not compare(source, clean | {"isExplicit": True})["explicit_ok"]
    assert compare(
        SONG | {"title": "Sunflower"},
        SONG | {"title": "Sunflower (Spider-Man: Into the Spider-Verse)"},
    )["exact"]
    assert not compare(
        SONG | {"title": "Song (Rivo Remix)"},
        SONG | {"title": "Song (Other Remix)"},
    )["version_ok"]


def test_review_is_protected_fenced_persistent_and_clearable(imported_service):
    service, imports, state, _ = imported_service
    source = SONG | {
        "title": "Instant Crush <studios>",
        "videoType": "MUSIC_VIDEO_TYPE_OMV",
    }
    state.update(tracks=[source], trackCount=1)
    owner = user(service)
    item_id = run(service, imports.preview, owner, URL, True)
    run(service, imports.refresh_one)
    entry = imports.get(owner, item_id)["entries"][0]
    assert entry["song_id"] is None
    assert entry["candidates"]
    candidate = entry["candidates"][0]
    key = identity(source)
    client = service[0]
    token = browser(service)
    path = f"/playlist-imports/{item_id}/choose"
    data = {"csrf": token, "source_key": key, "song_id": candidate["song_id"]}
    assert client.post(path, data=data | {"csrf": ""}).status_code == 403
    assert (
        client.post(path, data=data | {"song_id": "fake"}).status_code == 400
    )
    assert client.post(path, data=data).status_code == 200
    run(service, imports.refresh_one)
    entry = imports.get(owner, item_id)["entries"][0]
    assert entry["reviewed"] and entry["song_id"] == candidate["song_id"]
    imports.confirm(owner, item_id)
    stale = imports.repository.claim(item_id)
    progress = run(
        service, imports.provider.prepare, stale["source"], stale["progress"]
    )
    progress = run(service, imports.provider.batch, progress)
    assert imports.repository.checkpoint(stale, progress)
    imports.decide(owner, item_id, key, "")
    cleared = imports.get(owner, item_id)
    assert cleared["state"] == "running" and cleared["token"] == stale["token"]
    assert not cleared["entries"][0].get("reviewed")
    assert cleared["entries"][0]["song_id"] is None
    # Clearing now restores the entry directly. The running refresh keeps its
    # lease, but cannot restore a choice from an older in-flight snapshot.
    assert imports.repository.checkpoint(stale, progress)
    stale["progress"] = progress
    assert imports.repository.publish(
        stale, Snapshot("Chart", progress["entries"])
    )
    assert not imports.get(owner, item_id)["entries"][0].get("reviewed")
    token = browser(service, "alice")
    assert client.post(path, data=data | {"csrf": token}).status_code == 403


def test_equivalent_releases_and_distinct_durations():
    async def scenario(rows, source=SONG):
        calls = []

        async def fetch(payload):
            calls.append(payload)
            return {"songs": rows}

        result = await PlaylistResolver(
            fetch, lambda row: {"id": row["videoId"]}
        ).resolve(source)
        assert len(calls) <= 3
        return result

    explicit = SONG | {"isExplicit": True}
    clean = SONG | {"isExplicit": False, "videoId": "cleanaudio1"}
    duplicate = explicit | {"videoId": "duplicate01"}
    result = asyncio.run(scenario([clean, explicit, duplicate]))
    assert result["song"]["id"] == SONG["videoId"]
    result = asyncio.run(
        scenario(
            [
                explicit,
                duplicate | {"duration_seconds": 700},
            ]
        )
    )
    assert result["song"] is None
    assert result["reason"] == "Multiple recording versions"
    result = asyncio.run(scenario([explicit], SONG | {"isExplicit": False}))
    assert result["song"] is None
    assert result["candidates"][0]["mismatch"]


def test_review_mismatch_acknowledgment_and_source_change(imported_service):
    service, imports, state, _ = imported_service
    source = SONG | {"isExplicit": True, "videoType": "MUSIC_VIDEO_TYPE_OMV"}
    state.update(tracks=[source], trackCount=1)
    original = imports.provider.fetch

    async def fetch(payload):
        if payload["operation"] == "search":
            return {"songs": [SONG | {"isExplicit": False}]}
        return await original(payload)

    imports.provider.fetch = fetch
    owner = user(service)
    item_id = run(service, imports.preview, owner, URL, True)
    run(service, imports.refresh_one)
    entry = imports.get(owner, item_id)["entries"][0]
    assert entry["reason"] == "Explicitness mismatch"
    candidate = entry["candidates"][0]
    client = service[0]
    token = browser(service)
    path = f"/playlist-imports/{item_id}/choose"
    data = {
        "csrf": token,
        "source_key": identity(source),
        "song_id": candidate["song_id"],
    }
    assert client.post(path, data=data).status_code == 400
    assert (
        client.post(
            path,
            data=data | {"acknowledge": "yes"},
            headers={"origin": "https://evil.test"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            path,
            data=data | {"acknowledge": "yes"},
        ).status_code
        == 200
    )
    run(service, imports.refresh_one)
    imports.confirm(owner, item_id)
    state["tracks"] = [source | {"title": "Other recording"}]
    run(service, imports.refresh, owner, item_id)
    run(service, imports.refresh_one)
    row = imports.get(owner, item_id)
    assert not row["entries"][0].get("reviewed")
    assert not row["progress"]["decisions"]


def test_meaningful_unrecognized_decorations_are_preserved():
    assert not compare(
        SONG | {"title": "Song | Live"},
        SONG | {"title": "Song"},
    )["exact"]
    assert not compare(
        SONG | {"title": "Song (from Live Concert)"},
        SONG | {"title": "Song"},
    )["version_ok"]
