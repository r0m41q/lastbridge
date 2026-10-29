import asyncio
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from xml.etree.ElementTree import fromstring

import httpx
import pytest
import test_catalog
from test_catalog import body, get, search

import lastbridge.lyrics as lyrics_module
from lastbridge.accounts import Accounts
from lastbridge.lyrics import Lyrics, retry_after, structured, timed_lines

catalog_service = test_catalog.catalog_service

# Synthetic text: fractional precision, repeated stamps and blank cues.
LRC = (
    "[ar:Example]\n[offset:100]\n[00:01.2] First <&>\n"
    "[00:03.045][00:05] Repeat\n[00:06.00]\n"
)


def record(song, **changes):
    return {
        "artistName": song["artist"],
        "trackName": song["title"],
        "albumName": song["album"],
        "duration": song["duration"],
        "instrumental": False,
        "syncedLyrics": LRC,
        "plainLyrics": "First <&>\nRepeat",
        **changes,
    }


@pytest.fixture
def lyrics_service(catalog_service):
    service = catalog_service
    song = search(service)["song"][0]
    state = {"row": record(song), "status": 200, "calls": []}

    def respond(request):
        state["calls"].append(request)
        return httpx.Response(
            state["status"],
            json=state["row"],
            headers=state.get("headers"),
        )

    async def install():
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        service[1].state.lyrics.client = upstream
        return upstream

    client = service[0].portal.call(install)
    yield service, song, state
    service[0].portal.call(client.aclose)


def entries(service, song, **params):
    return body(get(service, "getLyricsBySongId", id=song["id"], **params))[
        "lyricsList"
    ]["structuredLyrics"]


def test_json_xml_post_cache_and_public_only_metadata(lyrics_service):
    service, song, state = lyrics_service
    result = entries(service, song)
    assert result[0]["synced"] is True
    assert result[0]["lang"] == "und" and result[0]["offset"] == 0
    assert result[0]["line"] == [
        {"start": 1100, "value": "First <&>"},
        {"start": 2945, "value": "Repeat"},
        {"start": 4900, "value": "Repeat"},
        {"start": 5900, "value": ""},
    ]
    response = get(service, "getLyricsBySongId.view", id=song["id"], f="xml")
    root = fromstring(response.content)
    ns = {"s": "http://subsonic.org/restapi"}
    line = root.find("s:lyricsList/s:structuredLyrics/s:line", ns)
    assert line.text == "First <&>"
    assert line.attrib == {"start": "1100"}
    response = service[0].post(
        "/rest/getLyricsBySongId.view",
        data={
            "apiKey": service[3]["alice"],
            "id": song["id"],
            "v": "1.16.1",
            "c": "Feishin",
            "f": "json",
        },
    )
    assert body(response)["lyricsList"]["structuredLyrics"] == result
    assert len(state["calls"]) == 1
    request = state["calls"][0]
    assert request.url.host == "lrclib.net"
    assert set(request.url.params) == {
        "artist_name",
        "track_name",
        "album_name",
        "duration",
    }
    assert request.url.params["track_name"] == song["title"]
    assert "Lastbridge/" in request.headers["user-agent"]
    assert "authorization" not in request.headers
    with service[1].state.accounts.connection() as connection:
        assert connection.execute("SELECT count(*) FROM lyrics").fetchone()[0]
        assert not connection.execute("SELECT * FROM favorites").fetchall()
        assert not connection.execute("SELECT * FROM save_jobs").fetchall()


@pytest.mark.parametrize(
    "change",
    [
        {"trackName": "Instant Crush <studio> (Live)"},
        {"trackName": "Instant Crush <studio> (Remix)"},
        {"artistName": "Someone else"},
        {"albumName": "Live at the venue"},
        {"duration": 340},
        {"duration": float("nan")},
        {"duration": 10**400},
        {"duration": True},
        {"artistName": None},
        {"albumName": ["RAM"]},
    ],
)
def test_mismatched_recording_is_not_used(lyrics_service, change):
    service, song, state = lyrics_service
    state["row"].update(change)
    # NaN cannot be encoded by httpx's strict JSON response encoder.
    if isinstance(change.get("duration"), float):
        assert structured(song, state["row"]) == []
    else:
        assert entries(service, song) == []
        assert entries(service, song) == []
        assert len(state["calls"]) == 1


def test_minor_case_whitespace_and_duration_difference(lyrics_service):
    service, song, state = lyrics_service
    state["row"].update(
        artistName="  " + song["artist"].upper() + "  ",
        duration=song["duration"] + 2,
    )
    assert entries(service, song)[0]["synced"]


@pytest.mark.parametrize(
    "source",
    [
        "[00:99] Invalid",
        "[99:59] Beyond recording",
        "[00:01] Good\nnot timestamped",
        "[offset:999999]\n[00:01] Too much offset",
        "[00:01] Bad\x00text",
        "[00:01] " + "x" * 2049,
        "[00:01] x\n" * 2001,
        "[00:01]" * 100 + "x" * 2048,
    ],
)
def test_malformed_lrc_falls_back_without_making_timing(source):
    song = {"artist": "A", "title": "T", "album": "B", "duration": 90}
    result = structured(song, record(song, syncedLyrics=source))
    assert result[0]["synced"] is False
    assert all("start" not in line for line in result[0]["line"])


def test_lrc_precision_order_negative_offset_and_duplicates():
    assert timed_lines(
        "[offset:-250]\n[00:02.001] Two\n"
        "[00:01.01] One\n[00:01.01] One\n[00:01.01] Other",
        10,
    ) == [
        {"start": 1260, "value": "One"},
        {"start": 1260, "value": "Other"},
        {"start": 2251, "value": "Two"},
    ]


@pytest.mark.parametrize("kind", ["instrumental", "missing", "plain"])
def test_missing_instrumental_and_plain_are_graceful(lyrics_service, kind):
    service, song, state = lyrics_service
    if kind == "instrumental":
        state["row"]["instrumental"] = True
    elif kind == "missing":
        state["status"] = 404
    else:
        state["row"]["syncedLyrics"] = None
    result = entries(service, song)
    if kind == "plain":
        assert result[0]["synced"] is False
        assert all("start" not in line for line in result[0]["line"])
    else:
        assert result == []
    assert entries(service, song) == result
    assert len(state["calls"]) == 1


def test_auth_parameter_errors_and_head_do_not_fetch(lyrics_service):
    service, song, state = lyrics_service
    for params, code in (
        ({"id": "unknown"}, 70),
        ({"id": ""}, 10),
        ({"id": song["albumId"]}, 70),
        ({"id": song["id"], "apiKey": service[3]["unlinked"]}, 50),
        ({"id": song["id"], "apiKey": "wrong"}, 44),
    ):
        assert (
            body(get(service, "getLyricsBySongId", **params))["error"]["code"]
            == code
        )
    response = service[0].head(
        "/rest/getLyricsBySongId",
        params={
            "id": song["id"],
            "apiKey": service[3]["admin"],
            "v": "1.16.1",
            "c": "test",
        },
    )
    assert response.status_code == 200
    assert not state["calls"]


def test_restart_cache_and_metadata_change_invalidate(lyrics_service):
    service, song, state = lyrics_service
    original = entries(service, song)
    app = service[1]

    async def restart():
        old = app.state.lyrics
        await old.close()
        app.state.lyrics = Lyrics(old.accounts, old.catalog, old.client)

    service[0].portal.call(restart)
    assert entries(service, song) == original
    assert len(state["calls"]) == 1
    row = app.state.catalog.read(song["id"])
    app.state.catalog.put(
        "song", row["source"], song | {"title": song["title"] + " (Live)"}
    )
    assert entries(service, song) == []
    assert len(state["calls"]) == 2


def expire(service, *, age=0):
    with service[1].state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE lyrics SET expires=0, updated=?", (time.time() - age,)
        )


def test_outage_stale_cache_and_rate_limit_cooldown(lyrics_service):
    service, song, state = lyrics_service
    good = entries(service, song)
    expire(service)
    state.update(status=429, headers={"Retry-After": "120"})
    assert entries(service, song) == good
    assert service[1].state.lyrics.blocked_until > time.monotonic() + 110
    assert len(state["calls"]) == 2
    # Other songs also respect the provider-wide cooldown.
    row = service[1].state.catalog.read(song["id"])
    other = service[1].state.catalog.put("song", "othertrack1", song)
    assert entries(service, other) == []
    assert len(state["calls"]) == 2
    service[1].state.lyrics.blocked_until = 0
    expire(service, age=31 * 86400)
    state["status"] = 500
    assert entries(service, song) == []
    assert row["source"]


def test_incomplete_metadata_does_not_request_provider(lyrics_service):
    service, song, state = lyrics_service
    row = service[1].state.catalog.read(song["id"])
    for changes in ({"duration": 0}, {"duration": 3601}, {"artist": ""}):
        service[1].state.catalog.put("song", row["source"], song | changes)
        assert entries(service, song) == []
    assert not state["calls"]


def test_pending_tasks_and_persistent_cache_are_bounded(lyrics_service):
    service, song, _ = lyrics_service
    lyrics = service[1].state.lyrics
    store = service[1].state.accounts

    async def scenario():
        release = asyncio.Event()

        async def respond(request):
            await release.wait()
            return httpx.Response(404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as client:
            lyrics.client = client
            songs = [
                service[1].state.catalog.put("song", str(i), song)
                for i in range(5)
            ]
            waiting = [
                asyncio.create_task(lyrics.get(item["id"]))
                for item in songs[:4]
            ]
            await asyncio.sleep(0)
            assert len(lyrics.tasks) == 4
            assert await lyrics.get(songs[4]["id"]) == []
            release.set()
            assert await asyncio.gather(*waiting) == [[], [], [], []]

    service[0].portal.call(scenario)
    with store.transaction() as connection:
        connection.execute("DELETE FROM lyrics")
        connection.executemany(
            "INSERT INTO catalog VALUES(?, 'song', ?, '{}', NULL, 0, 1)",
            [(f"limit-{i}", f"limit-{i}") for i in range(1000)],
        )
        connection.executemany(
            "INSERT INTO lyrics VALUES(?, 'test', '[]', 0, 0)",
            [(f"limit-{i}",) for i in range(1000)],
        )
    lyrics.save(song["id"], "test", [], 60)
    with store.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM lyrics").fetchone()[0]
            == 1000
        )


@pytest.mark.parametrize("status", [302, 500, 200])
def test_bad_provider_responses_are_cached_and_playback_unchanged(
    lyrics_service, status
):
    service, song, state = lyrics_service
    state.update(status=status, row=["malformed"])
    assert entries(service, song) == []
    assert entries(service, song) == []
    assert len(state["calls"]) == 1
    assert body(get(service, "getSong", id=song["id"]))["status"] == "ok"


def test_oversize_provider_response_is_rejected(lyrics_service):
    service, song, state = lyrics_service
    state["row"]["syncedLyrics"] = "x" * (lyrics_module.MAXIMUM + 1)
    assert entries(service, song) == []


def test_shared_requests_cancellation_deadline_and_shutdown(
    lyrics_service, monkeypatch
):
    service, song, _ = lyrics_service
    lyrics = service[1].state.lyrics

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def respond(request):
            calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json=record(song))

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as client:
            lyrics.client = client
            first = asyncio.create_task(lyrics.get(song["id"]))
            await entered.wait()
            second = asyncio.create_task(lyrics.get(song["id"]))
            await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            release.set()
            assert (await second)[0]["synced"]
            assert len(calls) == 1
            expire(service)
            release.clear()
            lyrics.next_request = 0
            monkeypatch.setattr(lyrics_module, "DEADLINE", 0.01)
            assert await lyrics.get(song["id"])
            expire(service)
            monkeypatch.setattr(lyrics_module, "DEADLINE", 20)
            task = asyncio.create_task(lyrics.get(song["id"]))
            await asyncio.sleep(0.01)
            await lyrics.close()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not lyrics.tasks

    service[0].portal.call(scenario)


def test_retry_after_seconds_and_date():
    assert retry_after("180") == 180
    assert retry_after("nan") == 60
    assert retry_after("garbage") == 60
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=180))
    assert 178 <= retry_after(future) <= 180


def test_schema_six_upgrade_preserves_accounts_catalog_and_radio(
    lyrics_service,
):
    service, song, _ = lyrics_service
    store = service[1].state.accounts
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO radio VALUES(?,?,?,?,?,?)",
            (store.user("admin")["id"], "example", song["id"], "[]", 1, 2),
        )
        connection.execute("DROP TABLE lyrics")
        connection.execute("PRAGMA user_version=6")
    migrated = Accounts(store.path.parent)
    assert migrated.client_user({"apiKey": service[3]["admin"]})
    assert service[1].state.catalog.read(song["id"])["document"] == song
    with migrated.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert connection.execute("SELECT * FROM radio").fetchone()["seed_id"]
        assert not connection.execute("SELECT * FROM lyrics").fetchall()
