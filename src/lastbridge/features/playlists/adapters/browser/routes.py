from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from lastbridge.features.playlists.adapters.browser.access import (
    require_playlist_access,
)
from lastbridge.features.playlists.adapters.browser.imports import (
    router as imports_router,
)
from lastbridge.features.playlists.adapters.browser.views import playlists_view
from lastbridge.shared.errors import CatalogError
from lastbridge.transport.browser.dependencies import (
    current_user,
    protected_form,
)
from lastbridge.transport.browser.rendering import Page, render
from lastbridge.transport.http.context import services

router = APIRouter()
router.include_router(imports_router)


@router.get("/playlists")
async def playlists_page(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    require_playlist_access(user)
    return render(
        "playlists/index.html",
        Page("Playlists", "/playlists", "dashboard"),
        user=user,
        **playlists_view(services(request), user),
    )


@router.post("/playlists/{action}")
async def new_playlist(request: Request, action: str):
    user, values = await protected_form(request)
    require_playlist_access(user)
    playlists = services(request).playlists
    try:
        if action == "create":
            await playlists.create(user, values.get("name"), [])
        elif action == "copy":
            await playlists.copy(
                user, values.get("source", ""), values.get("name")
            )
        else:
            raise HTTPException(404, "Action not found")
    except CatalogError as error:
        raise HTTPException(400, error.message) from None
    return RedirectResponse("/playlists", status_code=303)


@router.post("/playlists/{item_id}/{action}")
async def playlist_action(request: Request, item_id: str, action: str):
    user, _ = await protected_form(request)
    require_playlist_access(user)
    playlists = services(request).playlists
    try:
        if action in ("keep", "release"):
            await playlists.keep(user, item_id, action == "keep")
        elif action == "delete":
            await playlists.delete(user, item_id)
        else:
            raise HTTPException(404, "Action not found")
    except CatalogError as error:
        raise HTTPException(
            404 if error.code == 70 else 400, error.message
        ) from None
    return RedirectResponse("/playlists", status_code=303)
