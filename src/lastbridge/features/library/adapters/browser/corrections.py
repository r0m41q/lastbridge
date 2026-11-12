from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from lastbridge.shared.errors import CatalogError, StorageError
from lastbridge.transport.browser.dependencies import (
    current_user,
    protected_form,
)
from lastbridge.transport.browser.rendering import Page, render
from lastbridge.transport.http.context import services

router = APIRouter()


def failure(error):
    if isinstance(error, StorageError):
        return HTTPException(503, "Recording choices unavailable; retry")
    return HTTPException(
        404 if error.code == 70 else 503 if error.code == 0 else 400,
        error.message,
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
    return render(
        "library/recording.html",
        Page(
            "Choose recording",
            "/lastfm/sync" if kind == "love" else "/library",
            "dashboard",
        ),
        user=user,
        item=item,
        path=f"/library/recordings/{kind}/{key}",
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
        else:
            raise HTTPException(404, "Action not found")
    except (CatalogError, StorageError) as error:
        raise failure(error) from None
    return RedirectResponse(path, status_code=303)
