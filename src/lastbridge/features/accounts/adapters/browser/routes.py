import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from lastbridge.features.accounts.adapters.browser.views import overview
from lastbridge.features.accounts.domain.security import SESSION_SECONDS
from lastbridge.providers.lastfm.client import AUTH_VALUE
from lastbridge.shared.lastfm import LastfmError
from lastbridge.transport.browser.dependencies import (
    COOKIE,
    LOGIN_CSRF,
    check_csrf,
    current_user,
    protected_form,
)
from lastbridge.transport.browser.rendering import Page, message, render
from lastbridge.transport.http.context import services
from lastbridge.transport.http.parameters import parameters

router = APIRouter()


@router.get("/login")
async def login_page(request: Request):
    csrf = secrets.token_urlsafe(32)
    response = render(
        "accounts/login.html", Page("Sign in", layout="auth"), token=csrf
    )
    response.set_cookie(
        LOGIN_CSRF,
        csrf,
        max_age=600,
        httponly=True,
        samesite="strict",
        secure=request.app.state.config.public_url.startswith("https:"),
    )
    return response


@router.post("/login")
async def login(request: Request):
    values = await parameters(request)
    check_csrf(
        request, values.get("csrf", ""), request.cookies.get(LOGIN_CSRF, "")
    )
    username, password = values.get("username", ""), values.get("password", "")
    if len(username) > 64 or len(password) > 256:
        raise HTTPException(400, "Invalid sign-in")
    async with services(request).password_limit:
        token = await run_in_threadpool(
            services(request).accounts.login,
            username,
            password,
            request.client.host if request.client else "unknown",
        )
    if token is None:
        return render(
            "accounts/login_failed.html", Page("Sign-in failed"), 401
        )
    old_token = request.cookies.get(COOKIE)
    if old_token:
        services(request).accounts.logout(old_token)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE,
        token,
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.app.state.config.public_url.startswith("https:"),
    )
    response.delete_cookie(LOGIN_CSRF)
    return response


@router.post("/logout")
async def logout(request: Request):
    await protected_form(request)
    services(request).accounts.logout(request.cookies.get(COOKIE, ""))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@router.post("/admin/users")
async def create_user(request: Request):
    user, values = await protected_form(request)
    if not user["admin"]:
        raise HTTPException(403, "Administrator required")
    try:
        async with services(request).password_limit:
            await run_in_threadpool(
                services(request).accounts.create_user,
                values.get("username", ""),
                values.get("password", ""),
            )
    except ValueError as error:
        return message(Page("Account not created"), str(error), 400, user=user)
    return message(
        Page("User created"),
        user=user,
        back_url="/",
        back_label="Back to your account",
    )


@router.post("/account/client-password")
async def client_password(request: Request):
    user, _ = await protected_form(request)
    if user["link_status"] != "linked":
        raise HTTPException(403, "Link Last.fm first")
    value = services(request).accounts.rotate_client_secret(user["id"])
    return render(
        "accounts/client_password.html",
        Page("Client password"),
        user=user,
        secret=value,
    )


@router.post("/lastfm/link")
async def start_link(request: Request):
    user, _ = await protected_form(request)
    if not services(request).lastfm.configured:
        raise HTTPException(503, "Last.fm is not configured")
    state = services(request).accounts.start_link(user["token_hash"])
    callback = (
        request.app.state.config.public_url
        + "/lastfm/callback?"
        + (urlencode({"state": state}))
    )
    return RedirectResponse(
        services(request).lastfm.authorization_url(callback),
        status_code=303,
    )


@router.get("/lastfm/callback")
async def callback(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Sign in and start a new Last.fm link")
    values = await parameters(request)
    state, token = values.get("state", ""), values.get("token", "")
    if (
        not AUTH_VALUE.fullmatch(token)
        or len(state) != 43
        or not services(request).lastfm.configured
        or not services(request).accounts.claim_link(
            state, token, user["token_hash"]
        )
    ):
        raise HTTPException(400, "Invalid or expired link; start again")
    try:
        name, secret = await services(request).lastfm.exchange(
            services(request).http_client, token
        )
    except LastfmError:
        return render(
            "accounts/link_failed.html", Page("Link failed"), 502, user=user
        )
    if not services(request).accounts.finish_link(
        user["token_hash"], name, secret
    ):
        raise HTTPException(400, "Session expired; start a new Last.fm link")
    services(request).sync.linked(user["id"])
    services(request).mixes.linked(user["id"])
    # Remove the provider token and state from the address bar immediately.
    return RedirectResponse("/", status_code=303)


@router.get("/")
async def account(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return render(
        "accounts/overview.html",
        Page("Your account", "/", "dashboard"),
        user=user,
        **overview(services(request), user),
    )
