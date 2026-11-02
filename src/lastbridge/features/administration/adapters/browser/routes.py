from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from lastbridge.shared.errors import StorageError
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


@router.post("/admin/charts/{item_id}/retry")
async def retry_chart(request: Request, item_id: str):
    user, _ = await protected_form(request)
    administrator(user)
    services(request).administration.charts(item_id)
    return RedirectResponse("/admin", status_code=303)
