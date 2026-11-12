from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from lastbridge.shared.errors import CatalogError, StorageError
from lastbridge.transport.browser.dependencies import (
    current_user,
    protected_form,
)
from lastbridge.transport.browser.rendering import Page, message, render
from lastbridge.transport.http.context import services

router = APIRouter()


def saved_recording(request, user, song_id):
    library = services(request).library
    if not any(
        row["id"] == song_id
        for row in library.repository.downloads(user["id"])
    ):
        raise HTTPException(404, "Saved recording not found")
    return library.recording(user["id"], song_id, "song")


@router.get("/library/files/{song_id}")
async def saved_file(request: Request, song_id: str):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    svc = services(request)
    item = saved_recording(request, user, song_id)
    status = (
        svc.enrichment.report(item["source"], svc.library.policy)
        if svc.enrichment
        else None
    )
    return render(
        "library/file.html",
        Page("Saved file", "/library", "dashboard"),
        user=user,
        item=item,
        status=status,
    )


@router.post("/library/files/{song_id}/repair")
async def repair_file(request: Request, song_id: str):
    user, values = await protected_form(request)
    svc = services(request)
    item = saved_recording(request, user, song_id)
    if svc.enrichment is None:
        raise HTTPException(503, "Saved file repair unavailable")
    try:
        await svc.enrichment.process(
            item["source"],
            svc.library.policy,
            repair=True,
            embed_cover=values.get("embed_cover") == "yes",
            embed_lyrics=values.get("embed_lyrics") == "yes",
        )
        if svc.audio.ready_notifier is not None:
            await svc.audio.ready_notifier.notify()
    except (
        CatalogError,
        StorageError,
        OSError,
        ValueError,
        RuntimeError,
    ) as e:
        return message(
            Page("Saved file not repaired"),
            e.message
            if isinstance(e, CatalogError)
            else "Could not repair saved file; check storage and retry.",
            409,
            user=user,
            back_url=f"/library/files/{song_id}",
            back_label="Back to saved file",
        )
    return RedirectResponse(f"/library/files/{song_id}", status_code=303)
