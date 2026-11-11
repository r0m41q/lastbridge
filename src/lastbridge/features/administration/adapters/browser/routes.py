from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from lastbridge.shared.errors import CatalogError, StorageError
from lastbridge.transport.browser.dependencies import (
    current_user,
    protected_form,
)
from lastbridge.transport.browser.rendering import Page, message, render
from lastbridge.transport.http.context import services

router = APIRouter()


def administrator(user):
    if user is None or not user["admin"]:
        raise HTTPException(403, "Administrator required")
    return user


@router.get("/admin")
async def dashboard(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    administrator(user)
    try:
        report = await services(request).administration.dashboard()
    except StorageError, OSError:
        return message(
            Page("Administration"),
            "Storage unavailable; try again.",
            503,
            user=user,
        )
    return render(
        "administration/index.html",
        Page("Administration", "/admin", "dashboard"),
        user=user,
        **report,
    )


@router.post("/admin/users/{user_id}/{action}")
async def user_action(request: Request, user_id: int, action: str):
    user, values = await protected_form(request)
    administrator(user)
    admin = services(request).administration
    try:
        if action == "password":
            if values.get("confirm") != "yes":
                raise ValueError("Confirm the password reset.")
            target = admin.repository.user(user_id)
            if target is None:
                raise ValueError("Account does not exist.")
            async with services(request).password_limit:
                await run_in_threadpool(
                    services(request).accounts.reset_password,
                    target["username"],
                    values.get("password", ""),
                )
        else:
            admin.retry(user_id, action)
    except ValueError as error:
        return message(
            Page("Action not completed"),
            str(error),
            409,
            user=user,
            back_url="/admin",
            back_label="Back to administration",
        )
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/audio")
async def update_audio(request: Request):
    user, values = await protected_form(request)
    administrator(user)
    try:
        await services(request).administration.update_audio(values)
    except ValueError as error:
        return message(
            Page("Audio settings not saved"),
            str(error),
            409,
            user=user,
            back_url="/admin",
            back_label="Back to administration",
        )
    except StorageError:
        return message(
            Page("Audio settings not saved"),
            "Storage unavailable; try again.",
            503,
            user=user,
            back_url="/admin",
            back_label="Back to administration",
        )
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/charts/{item_id}/retry")
async def retry_chart(request: Request, item_id: str):
    user, _ = await protected_form(request)
    administrator(user)
    services(request).administration.charts(item_id)
    return RedirectResponse("/admin", status_code=303)


def action_failure(user, error):
    status = (
        {50: 403, 70: 404}.get(error.code, 400)
        if isinstance(error, CatalogError)
        else 503
    )
    return message(
        Page("Changes not saved"),
        error.message
        if isinstance(error, CatalogError)
        else "Storage unavailable; try again.",
        status,
        user=user,
        back_url="/admin",
        back_label="Back to administration",
    )


@router.post("/admin/stations")
async def new_station(request: Request):
    user, values = await protected_form(request)
    administrator(user)
    try:
        services(request).administration.stations.save(user, values)
    except (CatalogError, StorageError) as error:
        return action_failure(user, error)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/stations/{item_id}/{action}")
async def station_action(request: Request, item_id: str, action: str):
    user, values = await protected_form(request)
    administrator(user)
    stations = services(request).administration.stations
    try:
        if action == "save":
            stations.save(user, values, item_id)
        elif action == "delete":
            stations.remove(user, item_id)
        else:
            raise HTTPException(404, "Action not found")
    except (CatalogError, StorageError) as error:
        return action_failure(user, error)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/charts/{item_id}/rename")
async def rename_chart(request: Request, item_id: str):
    user, values = await protected_form(request)
    administrator(user)
    try:
        services(request).administration.rename_chart(
            user, item_id, values.get("name", "")
        )
    except (CatalogError, StorageError) as error:
        return action_failure(user, error)
    return RedirectResponse("/admin", status_code=303)


@router.get("/admin/import-queue")
async def import_queue(
    request: Request, offset: int = Query(0, ge=0, le=1000000)
):
    user = administrator(current_user(request))
    try:
        jobs = services(request).administration.import_queue.queue(
            user, offset, 51
        )
    except StorageError as error:
        return action_failure(user, error)
    return render(
        "administration/import_queue.html",
        Page("Playlist import queue", "/admin", "dashboard"),
        user=user,
        jobs=jobs[:50],
        offset=offset,
        more=len(jobs) > 50,
    )


@router.post("/admin/import-queue/{item_id}/{action}")
async def control_import(request: Request, item_id: str, action: str):
    user, _ = await protected_form(request)
    administrator(user)
    queue = services(request).administration.import_queue
    try:
        if action == "refresh":
            await queue.refresh(user, item_id)
        else:
            queue.control(user, item_id, action)
    except (CatalogError, StorageError) as error:
        return action_failure(user, error)
    return RedirectResponse("/admin/import-queue", status_code=303)
