from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from lastbridge.features.library.adapters.browser.review import (
    queue_model,
    recording_entry,
)
from lastbridge.shared.errors import CatalogError, StorageError
from lastbridge.transport.browser.dependencies import (
    current_user,
    protected_form,
)
from lastbridge.transport.browser.rendering import Page, render
from lastbridge.transport.browser.review import wants_json
from lastbridge.transport.http.context import services

router = APIRouter()


def failure(error):
    if isinstance(error, StorageError):
        return HTTPException(503, "Recording choices unavailable; retry")
    return HTTPException(
        404 if error.code == 70 else 503 if error.code == 0 else 400,
        error.message,
    )


@router.get("/library/recordings/loves")
async def love_queue(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        model = queue_model(services(request).corrections, user["id"])
    except (CatalogError, StorageError) as error:
        raise failure(error) from None
    if wants_json(request):
        return JSONResponse(model)
    return render(
        "library/recording.html",
        Page("Review loved tracks", "/lastfm/sync", "dashboard"),
        user=user,
        review=model,
    )


@router.get("/library/recordings/{kind}/{key}")
async def recording_page(request: Request, kind: str, key: str):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        item = services(request).corrections.report(user["id"], kind, key)
    except (CatalogError, StorageError) as error:
        raise failure(error) from None
    path = f"/library/recordings/{kind}/{key}"
    entry = recording_entry(item, path)
    if wants_json(request):
        return JSONResponse({"entry": entry})
    if kind == "love":
        model = queue_model(services(request).corrections, user["id"], key)
        model["entries"] = [
            entry if e["key"] == key else e for e in model["entries"]
        ]
        if not any(e["key"] == key for e in model["entries"]):
            model["entries"].append(entry)
    else:
        model = {
            "entries": [entry],
            "kind": kind,
            "editable": True,
            "running": False,
            "path": path,
            "selected": key,
        }
    return render(
        "library/recording.html",
        Page(
            "Choose recording",
            "/lastfm/sync" if kind == "love" else "/library",
            "dashboard",
        ),
        user=user,
        review=model,
    )


@router.post("/library/recordings/{kind}/{key}/{action}")
async def recording_action(request: Request, kind: str, key: str, action: str):
    user, values = await protected_form(request)
    corrections = services(request).corrections
    path = f"/library/recordings/{kind}/{key}"
    try:
        if action == "search":
            await corrections.search(
                user["id"], kind, key, values.get("query", "")
            )
        elif action in ("choose", "clear"):
            song_id = await corrections.choose(
                user["id"],
                kind,
                key,
                values.get("token", ""),
                values.get("song_id", "") if action == "choose" else "",
                values.get("acknowledge") == "yes",
            )
            path = f"/library/recordings/song/{song_id}"
        elif action in ("skip", "restore") and kind == "love":
            corrections.skip(user["id"], key, action == "skip")
        elif action == "undo":
            await corrections.undo(
                user["id"], kind, key, values.get("undo", "")
            )
        else:
            raise HTTPException(404, "Action not found")
        if wants_json(request):
            entry = recording_entry(
                corrections.report(user["id"], kind, key),
                f"/library/recordings/{kind}/{key}",
            )
            if action in ("choose", "clear"):
                entry.update(status="resolved", label="Confirmed")
            return JSONResponse(
                {
                    "entry": entry,
                    "undo": corrections.undo_token(user["id"]),
                    "key": key,
                }
            )
    except (CatalogError, StorageError) as error:
        raise failure(error) from None
    return RedirectResponse(path, status_code=303)
