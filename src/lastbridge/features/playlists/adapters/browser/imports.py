from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from lastbridge.shared.errors import CatalogError
from lastbridge.transport.browser.dependencies import (
    current_user,
    protected_form,
)
from lastbridge.transport.browser.rendering import Page, render
from lastbridge.transport.http.context import services

router = APIRouter()


def linked(user):
    if user["link_status"] not in ("linked", "relink"):
        raise HTTPException(403, "Link Last.fm first")


def failure(error):
    return HTTPException(
        404 if error.code == 70 else 403 if error.code == 50 else 400,
        error.message,
    )


@router.post("/playlist-imports/preview")
async def preview(request: Request):
    user, values = await protected_form(request)
    linked(user)
    if values.get("scope", "private") not in ("private", "shared"):
        raise HTTPException(400, "Invalid import visibility")
    try:
        item_id = await services(request).playlists.imports.preview(
            user, values.get("url", ""), values.get("scope") == "shared"
        )
    except CatalogError as error:
        raise failure(error) from None
    except TimeoutError:
        raise HTTPException(
            400, "Import timed out; try a smaller playlist"
        ) from None
    return RedirectResponse("/playlist-imports/" + item_id, status_code=303)


@router.get("/playlist-imports/{item_id}")
async def report(request: Request, item_id: str):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    linked(user)
    try:
        item = services(request).playlists.imports.get(user, item_id)
    except CatalogError as error:
        raise failure(error) from None
    return render(
        "playlists/import.html",
        Page("Playlist import", "/playlists", "dashboard"),
        user=user,
        item=item,
        updated=datetime.fromtimestamp(item["updated"], UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
        next_refresh=datetime.fromtimestamp(
            item["next_attempt"], UTC
        ).strftime("%Y-%m-%d %H:%M UTC"),
        playable=sum(bool(e.get("song_id")) for e in item["entries"]),
    )


@router.post("/playlist-imports/{item_id}/{action}")
async def change(request: Request, item_id: str, action: str):
    user, _ = await protected_form(request)
    linked(user)
    imports = services(request).playlists.imports
    target = "/playlists"
    try:
        if action == "confirm":
            imports.confirm(user, item_id)
        elif action == "refresh":
            await imports.refresh(user, item_id)
            target = "/playlist-imports/" + item_id
        elif action == "delete":
            imports.remove(user, item_id)
        else:
            raise HTTPException(404, "Action not found")
    except CatalogError as error:
        raise failure(error) from None
    return RedirectResponse(target, status_code=303)
