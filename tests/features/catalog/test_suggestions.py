import json

import pytest
from support.catalog import SONG, body, get
from support.suggestions import SuggestionMusic, response, suggestion

from lastbridge.features.catalog.adapters.youtube.suggestions import (
    recommendations,
    search,
)
from lastbridge.features.catalog.adapters.youtube.worker import execute


def test_worker_prioritizes_entities_and_deduplicates_ids(monkeypatch):
    first = SONG | {"videoId": "firstsong01", "title": "Hotel California"}
    second = SONG | {"videoId": "secondsong1", "title": "Hotel Lobby"}
    # Same title with another ID remains a distinct recording.
    other = first | {"videoId": "othersong01"}
    music = SuggestionMusic(
        response(suggestion(first), suggestion(second), suggestion(first)),
        {"songs": [SONG, first, other]},
    )
    original_request = music._session.request
    monkeypatch.setattr("ytmusicapi.YTMusic", lambda: music)
    rows = execute(
        {"operation": "search", "query": "hotel", "kinds": ["songs"]}
    )["songs"]
    assert [r["videoId"] for r in rows] == [
        first["videoId"],
        second["videoId"],
        SONG["videoId"],
        other["videoId"],
    ]
    assert rows[0]["duration_seconds"] == SONG["duration_seconds"]
    assert rows[1]["duration_seconds"] == 337
    assert ("get_song", first["videoId"]) not in music.calls
    assert ("get_song", second["videoId"]) in music.calls
    assert music._session.request is original_request
    assert "private-tracking-value" not in json.dumps(rows)


def test_all_entity_categories_preserve_suggestion_order():
    music = SuggestionMusic(
        response(suggestion(kind="artists"), suggestion(kind="albums"))
    )
    result = search(music, "Daft", ["artists", "albums"])
    assert result["artists"][0]["id"] == SONG["artists"][0]["id"]
    assert result["albums"][0]["browseId"] == SONG["album"]["id"]
    assert not any(c[0] == "get_song" for c in music.calls)


@pytest.mark.parametrize("explicit", [True, False])
def test_prediction_retains_explicit_badge(explicit):
    item = suggestion()
    item["musicResponsiveListItemRenderer"]["badges"] = (
        [
            {
                "musicInlineBadgeRenderer": {
                    "icon": {"iconType": "MUSIC_EXPLICIT_BADGE"}
                }
            }
        ]
        if explicit
        else []
    )
    result = recommendations(response(item))
    assert result["songs"][0]["isExplicit"] is explicit


def test_no_requested_categories_make_no_requests():
    music = SuggestionMusic()
    assert search(music, "hotel", []) == {}
    assert music.calls == []


@pytest.mark.parametrize(
    "recommended",
    [
        TimeoutError("private error"),
        {},
        {"contents": None},
        response({"searchSuggestionRenderer": {"suggestion": "hotel"}}),
    ],
)
def test_missing_or_failed_predictions_keep_regular_results(recommended):
    music = SuggestionMusic(recommended)
    request = music._session.request
    assert search(music, "hotel", ["songs"])["songs"] == [SONG]
    assert music._session.request is request


def test_malformed_entities_do_not_hide_valid_recommendations():
    video = SONG | {"videoType": "MUSIC_VIDEO_TYPE_OMV"}
    missing_album = suggestion()
    missing_album["musicResponsiveListItemRenderer"]["flexColumns"].pop()
    result = recommendations(
        response(
            suggestion(video),
            missing_album,
            {"musicResponsiveListItemRenderer": {"flexColumns": [None]}},
            suggestion(),
        )
    )
    assert len(result["songs"]) == 1
    assert result["songs"][0]["videoId"] == SONG["videoId"]


@pytest.mark.parametrize(
    "details",
    [
        TimeoutError(),
        {},
        {"videoId": "wrongsource", "lengthSeconds": "200"},
        {"videoId": "suggested01", "lengthSeconds": "0"},
    ],
)
def test_unresolved_song_keeps_regular_results(details):
    row = SONG | {"videoId": "suggested01"}
    music = SuggestionMusic(response(suggestion(row)))
    music.details[row["videoId"]] = details
    assert search(music, "hotel", ["songs"])["songs"] == [SONG]


def test_suggestion_and_result_limits():
    rows = [SONG | {"videoId": f"track{i:06d}"} for i in range(110)]
    music = SuggestionMusic(
        response(*(suggestion(r) for r in rows[100:110])), {"songs": rows}
    )
    result = search(music, "hotel", ["songs"])["songs"]
    assert len(result) == 100
    assert [r["videoId"] for r in result[:5]] == [
        r["videoId"] for r in rows[100:105]
    ]
    assert sum(c[0] == "get_song" for c in music.calls) == 5


def test_feishin_paging_keeps_recommendations_first(
    catalog_service, monkeypatch
):
    row = SONG | {"videoId": "suggested01", "title": "Hotel California"}
    music = SuggestionMusic(response(suggestion(row)))
    provider = catalog_service[2]
    calls = []

    async def fetch(payload):
        calls.append(payload)
        return search(music, payload["query"], payload["kinds"])

    monkeypatch.setattr(provider, "fetch", fetch)
    params = {"query": "hotel", "artistCount": 0, "albumCount": 0}
    first = body(get(catalog_service, "search3", songCount=1, **params))[
        "searchResult3"
    ]["song"]
    second = body(
        get(catalog_service, "search3", songOffset=1, songCount=1, **params)
    )["searchResult3"]["song"]
    assert first[0]["title"] == "Hotel California"
    assert first[0]["duration"] == 337
    assert first[0]["albumId"]
    assert first[0]["contentType"]
    assert second[0]["title"] == SONG["title"]
    assert len(calls) == 1
