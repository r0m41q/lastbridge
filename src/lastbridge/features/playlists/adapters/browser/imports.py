from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from lastbridge.features.playlists.adapters.browser.access import (
    require_playlist_access,
)
from lastbridge.features.playlists.adapters.browser.review import (
    review_model,
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
    return HTTPException(
        404 if error.code == 70 else 403 if error.code == 50 else 400,
        error.message,
    )


@router.post("/playlist-imports/preview")
async def preview(request: Request):
    user, values = await protected_form(request)
    require_playlist_access(user)
    if values.get("scope", "private") not in ("private", "shared"):
        raise HTTPException(400, "Invalid import visibility")
    try:
        item_id = await services(request).playlists.imports.preview(
            user, values.get("url", ""), values.get("scope") == "shared"
        )
    except CatalogError as error:
        raise failure(error) from None
    except StorageError:
        raise HTTPException(503, "Import queue storage unavailable") from None
    return RedirectResponse("/playlist-imports/" + item_id, status_code=303)


@router.get("/playlist-imports/{item_id}")
async def report(request: Request, item_id: str):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    require_playlist_access(user)
    try:
        item = services(request).playlists.imports.get(user, item_id)
    except CatalogError as error:
        raise failure(error) from None
    except StorageError:
        raise HTTPException(503, "Import queue storage unavailable") from None
    model = review_model(item, user)
    if wants_json(request):
        return JSONResponse(model)
    entries = (
        item["progress"].get("entries", [])
        if not item["ready"] and item["state"] != "complete"
        else item["entries"]
    )
    return render(
        "playlists/import.html",
        Page("Playlist import", "/playlists", "dashboard"),
        user=user,
        item=item,
        review=model,
        updated=datetime.fromtimestamp(item["updated"], UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
        next_refresh=datetime.fromtimestamp(
            item["next_attempt"], UTC
        ).strftime("%Y-%m-%d %H:%M UTC"),
        playable=sum(bool(e.get("song_id")) for e in entries),
        entry_count=len(entries),
        entries=entries,
    )


@router.post("/playlist-imports/{item_id}/{action}")
async def change(request: Request, item_id: str, action: str):
    user, values = await protected_form(request)
    require_playlist_access(user)
    imports = services(request).playlists.imports
    target = "/playlists"
    try:
        if action in ("choose", "skip", "restore", "undo"):
            ticket = imports.decide(
                user,
                item_id,
                values.get("source_key", ""),
                values.get("song_id", ""),
                values.get("acknowledge") == "yes",
                action,
                values.get("undo", ""),
            )
            target = "/playlist-imports/" + item_id
            if wants_json(request):
                return JSONResponse(
                    {
                        "model": review_model(
                            imports.get(user, item_id), user
                        ),
                        "undo": ticket,
                        "key": values.get("source_key", ""),
                    }
                )
        elif action == "confirm":
            imports.confirm(user, item_id)
        elif action == "refresh":
            await imports.refresh(user, item_id)
            target = "/playlist-imports/" + item_id
        elif action == "schedule":
            imports.schedule(user, item_id, values.get("days", ""))
            target = "/playlist-imports/" + item_id
        elif action in ("pause", "resume", "cancel", "retry"):
            imports.control(user, item_id, action)
            target = "/playlist-imports/" + item_id
        elif action == "delete":
            imports.remove(user, item_id)
        else:
            raise HTTPException(404, "Action not found")
    except CatalogError as error:
        raise failure(error) from None
    except StorageError:
        raise HTTPException(503, "Import queue storage unavailable") from None
    return RedirectResponse(target, status_code=303)
