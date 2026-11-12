from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from lastbridge.features.library.adapters.browser.corrections import (
    router as corrections_router,
)
from lastbridge.features.library.adapters.browser.enrichment import (
    router as enrichment_router,
)
from lastbridge.transport.browser.dependencies import (
    current_user,
    protected_form,
)
from lastbridge.transport.browser.rendering import Page, render
from lastbridge.transport.http.context import services

router = APIRouter()
router.include_router(corrections_router)
router.include_router(enrichment_router)


@router.get("/library")
async def library_page(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    svc = services(request)
    downloads = svc.library.downloads(user["id"])
    if svc.enrichment is not None:
        for item in downloads:
            source = svc.library.recording(user["id"], item["id"])["source"]
            item["file"] = svc.enrichment.report(source, svc.library.policy)
    return render(
        "library/index.html",
        Page("Saved library", "/library", "dashboard"),
        user=user,
        downloads=downloads,
    )


@router.post("/library/retry")
async def retry_downloads(request: Request):
    user, _ = await protected_form(request)
    if user["link_status"] != "linked":
        raise HTTPException(403, "Link Last.fm first")
    services(request).library.retry(user["id"])
    return RedirectResponse("/library", status_code=303)
