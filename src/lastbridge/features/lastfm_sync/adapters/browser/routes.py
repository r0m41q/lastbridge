from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from lastbridge.features.lastfm_sync.adapters.browser.views import sync_view
from lastbridge.transport.browser.dependencies import (
    current_user,
    protected_form,
)
from lastbridge.transport.browser.rendering import Page, render

router = APIRouter()


@router.get("/lastfm/sync")
async def sync_page(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return render(
        "lastfm_sync/index.html",
        Page("Last.fm sync", "/lastfm/sync", "dashboard"),
        user=user,
        **sync_view(request.app.state.sync, user),
    )


@router.post("/lastfm/sync/{action}")
async def sync_action(request: Request, action: str):
    user, _ = await protected_form(request)
    if action != "pause" and user["link_status"] != "linked":
        raise HTTPException(403, "Link Last.fm first")
    try:
        request.app.state.sync.control(user["id"], action)
    except ValueError:
        raise HTTPException(400, "Unknown sync action") from None
    return RedirectResponse("/lastfm/sync", status_code=303)
