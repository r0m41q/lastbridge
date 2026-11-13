from functools import partial

import pytest
from support.catalog import get
from support.corrections import import_love, search_recordings
from support.imports import browser
from support.library import run, user_id

from lastbridge.shared.errors import CatalogError

JSON = {"Accept": "application/json"}


def test_love_skip_restore_undo_and_refresh_never_add_or_unlove(
    correction_service,
):
    service, _, _ = correction_service
    key = import_love(service)
    services = service[1].state.services
    owner = user_id(service)
    token = browser(service)
    path = f"/library/recordings/love/{key}"

    def change(action, **values):
        response = service[0].post(
            path + "/" + action,
            headers=JSON,
            data={"csrf": token, **values},
        )
        assert response.status_code == 200, response.text
        assert not response.history
        return response.json()

    skipped = change("skip")
    assert skipped["entry"]["status"] == "skipped"
    services.sync.repository.control(owner, "retry-matches")
    services.sync.repository.control(owner, "refresh")
    job = services.sync.repository.claim_import()[0]
    services.sync.repository.save_page(
        job, [("Last.fm artist", "Missing song")], 1
    )
    queue = service[0].get("/library/recordings/loves", headers=JSON).json()
    assert queue["entries"][0]["skipped"]
    assert change("restore")["entry"]["status"] == "review"
    assert change("skip")["entry"]["status"] == "skipped"
    # Undo only changes the review state, never import membership.
    saved = change("restore")
    assert change("undo", undo=saved["undo"])["entry"]["skipped"]
    assert not services.library.stars(owner)
    with services.database.connection() as connection:
        assert not connection.execute("SELECT * FROM love_outbox").fetchall()
        assert not connection.execute("SELECT * FROM save_jobs").fetchall()
        assert not connection.execute("SELECT * FROM love_removals").fetchall()


def test_skip_fences_an_in_flight_automatic_love(correction_service):
    service, original, _ = correction_service
    key = import_love(service)
    services = service[1].state.services
    owner = user_id(service)
    with services.database.transaction() as connection:
        connection.execute("UPDATE loved_items SET state='pending'")
    job, item = services.sync.repository.claim_import()
    services.corrections.skip(owner, key)
    assert not run(
        service,
        partial(
            services.library.change,
            owner,
            [(original["id"], "song")],
            True,
            imported=(job["target"], key),
        ),
    )
    services.sync.repository.resolved(
        job, item, "matched", None, original["id"]
    )
    assert services.corrections.report(owner, "love", key)["review_skipped"]
    services.corrections.skip(owner, key, False)
    # A lookup started before skip/restore still cannot add this favorite.
    assert not run(
        service,
        partial(
            services.library.change,
            owner,
            [(original["id"], "song")],
            True,
            imported=(job["target"], key),
        ),
    )
    assert not services.library.stars(owner)


@pytest.mark.parametrize("kind", ["song", "love"])
def test_recording_choice_can_be_undone_without_lastfm_changes(
    correction_service, kind
):
    service, original, _ = correction_service
    services = service[1].state.services
    owner = user_id(service)
    if kind == "song":
        get(service, "star", id=original["id"])
        key = original["id"]
    else:
        key = import_love(service)
    before = services.library.stars(owner)
    with services.database.connection() as connection:
        outbox = [
            dict(row)
            for row in connection.execute("SELECT * FROM love_outbox")
        ]
    token, path, review = search_recordings(correction_service, kind, key)
    response = service[0].post(
        path + "/choose",
        headers=JSON,
        data={
            "csrf": token,
            "token": review["token"],
            "acknowledge": "yes",
            "song_id": review["candidates"][0]["song_id"],
        },
    )
    assert response.status_code == 200, response.text
    ticket = response.json()["undo"]
    response = service[0].post(
        path + "/undo",
        headers=JSON,
        data={"csrf": token, "undo": ticket},
    )
    assert response.status_code == 200, response.text
    assert services.library.stars(owner) == before
    if kind == "song":
        assert (
            services.library.recording(owner, original["id"])["recording_id"]
            == original["id"]
        )
    else:
        assert services.corrections.report(owner, kind, key)["song_id"] is None
    with services.database.connection() as connection:
        assert [
            dict(row)
            for row in connection.execute("SELECT * FROM love_outbox")
        ] == outbox
    assert (
        service[0]
        .post(
            path + "/undo",
            headers=JSON,
            data={"csrf": token, "undo": ticket},
        )
        .status_code
        == 400
    )


def test_undo_after_unlike_cannot_resurrect_favorite(correction_service):
    service, original, _ = correction_service
    services = service[1].state.services
    get(service, "star", id=original["id"])
    token, path, review = search_recordings(correction_service)
    response = service[0].post(
        path + "/choose",
        headers=JSON,
        data={
            "csrf": token,
            "token": review["token"],
            "song_id": review["candidates"][0]["song_id"],
        },
    )
    ticket = response.json()["undo"]
    get(service, "unstar", id=original["id"])
    assert (
        service[0]
        .post(
            path + "/undo",
            headers=JSON,
            data={"csrf": token, "undo": ticket},
        )
        .status_code
        == 400
    )
    assert not services.library.stars(user_id(service))


@pytest.mark.parametrize("restriction", ["removed", "account", "other-user"])
def test_skip_restore_respects_removals_and_account(
    correction_service, restriction
):
    service, _, _ = correction_service
    key = import_love(service)
    services = service[1].state.services
    owner = user_id(service)
    services.corrections.skip(owner, key)
    with services.database.transaction() as connection:
        if restriction == "removed":
            connection.execute(
                "INSERT INTO love_removals VALUES(?,?)", (owner, key)
            )
        elif restriction == "account":
            connection.execute(
                "UPDATE users SET lastfm_name='another' WHERE id=?", (owner,)
            )
    target = (
        user_id(service, "alice") if restriction == "other-user" else owner
    )
    with pytest.raises(CatalogError):
        services.corrections.skip(target, key, False)
    assert not services.corrections.queue(target)


def test_review_skip_survives_restart_and_v21_migration(correction_service):
    from support.build_accounts import make_accounts
    from support.build_library import make_library

    service, original, _ = correction_service
    services = service[1].state.services
    owner = user_id(service)
    key = import_love(service)
    get(service, "star", id=original["id"])
    before = services.library.stars(owner)
    with services.database.transaction() as connection:
        connection.execute("DROP TABLE recording_review_undo")
        connection.execute(
            "ALTER TABLE loved_items DROP COLUMN review_skipped"
        )
        connection.execute("PRAGMA user_version=21")
    accounts = make_accounts(services.database.path.parent)
    library = make_library(accounts, services.catalog, "aac")
    assert library.stars(owner) == before
    assert not library.repository.correction_context(owner, "love", key)[
        "review_skipped"
    ]
    library.repository.skip_love_review(owner, key, True)
    reopened = make_library(
        make_accounts(services.database.path.parent), services.catalog, "aac"
    )
    assert reopened.repository.correction_context(owner, "love", key)[
        "review_skipped"
    ]
    assert reopened.stars(owner) == before
    with services.database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 23
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_successful_automatic_import_and_result_commit_together(
    correction_service,
):
    from functools import partial

    service, original, _ = correction_service
    services = service[1].state.services
    owner = user_id(service)
    key = import_love(service)
    with services.database.transaction() as connection:
        connection.execute("UPDATE loved_items SET state='pending'")
    job, _ = services.sync.repository.claim_import()
    assert run(
        service,
        partial(
            services.library.change,
            owner,
            [(original["id"], "song")],
            True,
            imported=(job["target"], key),
        ),
    )
    # Simulate the worker not yet calling resolved after membership committed.
    with pytest.raises(CatalogError, match="already a favorite"):
        services.corrections.skip(owner, key)
    item = services.corrections.report(owner, "love", key)
    assert item["song_id"] == original["id"] and item["state"] == "matched"


def test_love_json_actions_enforce_csrf_and_origin(correction_service):
    service, _, _ = correction_service
    key = import_love(service)
    token = browser(service)
    path = f"/library/recordings/love/{key}"
    assert (
        service[0]
        .post(path + "/skip", headers=JSON, data={"csrf": "wrong"})
        .status_code
        == 403
    )
    assert (
        service[0]
        .post(
            path + "/skip",
            headers=JSON | {"Origin": "https://foreign.test"},
            data={"csrf": token},
        )
        .status_code
        == 403
    )
    assert (
        service[0].get(path, headers=JSON).json()["entry"]["status"]
        == "review"
    )
