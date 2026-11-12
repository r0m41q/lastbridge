from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from lastbridge.features.library.adapters.browser.corrections import (
    router as corrections_router,
)
from lastbridge.transport.browser.dependencies import (
    current_user,
    protected_form,
)
from lastbridge.transport.browser.rendering import Page, render
from lastbridge.transport.http.context import services

router = APIRouter()
router.include_router(corrections_router)


@router.get("/library")
async def library_page(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return render(
        "library/index.html",
        Page("Saved library", "/library", "dashboard"),
        user=user,
        downloads=services(request).library.downloads(user["id"]),
    )


@router.post("/library/retry")
async def retry_downloads(request: Request):
    user, _ = await protected_form(request)
    if user["link_status"] != "linked":
        raise HTTPException(403, "Link Last.fm first")
    services(request).library.retry(user["id"])
    return RedirectResponse("/library", status_code=303)
