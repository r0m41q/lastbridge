from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from lastbridge.features.discovery.adapters.browser.views import mixes_view
from lastbridge.transport.browser.dependencies import (
    current_user,
    protected_form,
)
from lastbridge.transport.browser.rendering import Page, message, render
from lastbridge.transport.http.context import services

router = APIRouter()


@router.get("/mixes")
async def mixes_page(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return render(
        "discovery/index.html",
        Page("Personal mixes", "/mixes", "dashboard"),
        user=user,
        **mixes_view(services(request).mixes, user),
    )


@router.post("/mixes/refresh")
async def refresh_mixes(request: Request):
    user, _ = await protected_form(request)
    if user["link_status"] != "linked":
        raise HTTPException(403, "Link Last.fm first")
    try:
        services(request).mixes.request(user["id"])
    except ValueError as error:
        return message(
            Page("Refresh not queued"),
            str(error),
            409,
            user=user,
            back_url="/mixes",
            back_label="Back to mixes",
        )
    return RedirectResponse("/mixes", status_code=303)
