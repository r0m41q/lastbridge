from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from lastbridge.features.radio.adapters.browser.views import radio_view
from lastbridge.shared.errors import CatalogError
from lastbridge.transport.browser.dependencies import (
    current_user,
    protected_form,
)
from lastbridge.transport.browser.rendering import Page, message, render

router = APIRouter()


@router.get("/radio")
async def radio_page(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return render(
        "radio/index.html",
        Page("Radio", "/radio", "panel"),
        user=user,
        **radio_view(request.app.state, user),
    )


@router.post("/radio")
async def radio_action(request: Request):
    user, values = await protected_form(request)
    if user["link_status"] not in ("linked", "relink"):
        raise HTTPException(403, "Link Last.fm first")
    try:
        await request.app.state.radio.songs(user, values.get("id", ""), 50)
    except CatalogError as failure:
        return message(
            Page("Radio unavailable"),
            failure.message,
            503,
            user=user,
            back_url="/radio",
            back_label="Back to radio",
        )
    return RedirectResponse("/radio", status_code=303)
