import hmac
import secrets
from html import escape
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from lastbridge.accounts import SESSION_SECONDS
from lastbridge.lastfm import HEX_KEY, LastfmError

router = APIRouter()
COOKIE = "lastbridge_session"
LOGIN_CSRF = "lastbridge_login_csrf"


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)} · Lastbridge</title><body>"
        f"<h1>{escape(title)}</h1>{body}</body></html>",
        status_code=status,
    )


def csrf_field(value: str) -> str:
    return f'<input type="hidden" name="csrf" value="{escape(value)}">'


def form(action: str, csrf: str, label: str) -> str:
    return (
        f'<form method="post" action="{action}">{csrf_field(csrf)}'
        f'<button type="submit">{escape(label)}</button></form>'
    )


async def parameters(request: Request) -> dict[str, str]:
    pairs = list(request.query_params.multi_items())
    if request.method == "POST":
        if request.headers.get("content-type", "").split(";")[0] != (
            "application/x-www-form-urlencoded"
        ):
            raise HTTPException(415, "Use a URL-encoded form")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, "Form too large")
        try:
            pairs.extend(
                parse_qsl(
                    body.decode(), keep_blank_values=True, max_num_fields=32
                )
            )
        except ValueError, UnicodeError:
            raise HTTPException(400, "Invalid form") from None
    result = {}
    for key, value in pairs:
        if key in result or len(value) > 2048:
            raise HTTPException(400, "Duplicate or oversized parameter")
        result[key] = value
    return result


def current_user(request: Request):
    return request.app.state.accounts.session(request.cookies.get(COOKIE, ""))


async def protected_form(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Sign in through /login")
    values = await parameters(request)
    check_csrf(request, values.get("csrf", ""), user["csrf"])
    return user, values


def check_csrf(request: Request, supplied: str, expected: str) -> None:
    origin = request.headers.get("origin")
    if (
        not expected
        or not hmac.compare_digest(supplied.encode(), expected.encode())
        or (
            origin is not None
            and origin != request.app.state.config.public_url
        )
    ):
        raise HTTPException(403, "Invalid form; reload and try again")


@router.get("/login")
async def login_page(request: Request):
    csrf = secrets.token_urlsafe(32)
    response = page(
        "Sign in",
        '<form method="post" action="/login">'
        + csrf_field(csrf)
        + '<label>Username <input name="username" required maxlength="64" '
        'autocomplete="username"></label><br>'
        '<label>Password <input type="password" name="password" required '
        'maxlength="256" autocomplete="current-password"></label><br>'
        '<button type="submit">Sign in</button></form>',
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
    async with request.app.state.password_limit:
        token = await run_in_threadpool(
            request.app.state.accounts.login,
            username,
            password,
            request.client.host if request.client else "unknown",
        )
    if token is None:
        return page(
            "Sign-in failed",
            "<p>Invalid credentials or too many "
            'attempts. Wait a minute and <a href="/login">retry</a>.</p>',
            401,
        )
    old_token = request.cookies.get(COOKIE)
    if old_token:
        request.app.state.accounts.logout(old_token)
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


@router.get("/")
async def account(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    linked = user["link_status"] == "linked"
    body = f"<p>Signed in as {escape(user['username'])}.</p>"
    if linked:
        body += f"<p>Last.fm: {escape(user['lastfm_name'])} (linked).</p>"
    elif user["link_status"] == "relink":
        body += (
            "<p>Last.fm authorization was revoked. Link again to continue.</p>"
        )
    else:
        body += "<p>Link Last.fm before connecting a music client.</p>"
    if request.app.state.lastfm.configured:
        body += form(
            "/lastfm/link",
            user["csrf"],
            "Relink Last.fm" if linked else "Link Last.fm",
        )
    else:
        body += "<p>Ask the administrator to configure Last.fm keys.</p>"
    if linked:
        body += (
            "<p>Use your username and a generated client password in music "
            "apps. The same value can be used as an OpenSubsonic API key. "
            "Generating a new one disconnects clients using the old value.</p>"
        )
        body += form(
            "/account/client-password",
            user["csrf"],
            "Generate replacement client password"
            if user["client_secret"]
            else "Generate client password",
        )
    if user["admin"]:
        body += (
            '<h2>Create user</h2><form method="post" action="/admin/users">'
        )
        body += csrf_field(user["csrf"])
        body += (
            '<label>Username <input name="username" required maxlength="64" '
            'autocomplete="off"></label><br><label>Initial browser password '
            '<input type="password" name="password" minlength="12" '
            'maxlength="256" required autocomplete="new-password"></label><br>'
            '<button type="submit">Create user</button></form>'
        )
    body += form("/logout", user["csrf"], "Sign out")
    return page("Your account", body)


@router.post("/logout")
async def logout(request: Request):
    await protected_form(request)
    request.app.state.accounts.logout(request.cookies.get(COOKIE, ""))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@router.post("/admin/users")
async def create_user(request: Request):
    user, values = await protected_form(request)
    if not user["admin"]:
        raise HTTPException(403, "Administrator required")
    try:
        async with request.app.state.password_limit:
            await run_in_threadpool(
                request.app.state.accounts.create_user,
                values.get("username", ""),
                values.get("password", ""),
            )
    except ValueError as error:
        return page("Account not created", f"<p>{escape(str(error))}</p>", 400)
    return page("User created", '<a href="/">Back to your account</a>')


@router.post("/account/client-password")
async def client_password(request: Request):
    user, _ = await protected_form(request)
    if user["link_status"] != "linked":
        raise HTTPException(403, "Link Last.fm first")
    value = request.app.state.accounts.rotate_client_secret(user["id"])
    return page(
        "Client password",
        "<p>Copy this value now. It will not be "
        f"displayed again.</p><code>{escape(value)}</code>"
        '<p><a href="/">Back to your account</a></p>',
    )


@router.post("/lastfm/link")
async def start_link(request: Request):
    user, _ = await protected_form(request)
    if not request.app.state.lastfm.configured:
        raise HTTPException(503, "Last.fm is not configured")
    state = request.app.state.accounts.start_link(user["token_hash"])
    callback = (
        request.app.state.config.public_url
        + "/lastfm/callback?"
        + (urlencode({"state": state}))
    )
    return RedirectResponse(
        request.app.state.lastfm.authorization_url(callback), status_code=303
    )


@router.get("/lastfm/callback")
async def callback(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Sign in and start a new Last.fm link")
    values = await parameters(request)
    state, token = values.get("state", ""), values.get("token", "")
    if (
        not HEX_KEY.fullmatch(token)
        or len(state) != 43
        or not request.app.state.lastfm.configured
        or not request.app.state.accounts.claim_link(
            state, token, user["token_hash"]
        )
    ):
        raise HTTPException(400, "Invalid or expired link; start again")
    try:
        name, secret = await request.app.state.lastfm.exchange(
            request.app.state.http_client, token
        )
    except LastfmError:
        return page(
            "Link failed",
            "<p>Last.fm did not complete the link. "
            'Your existing link is kept. <a href="/">Start again</a>.</p>',
            502,
        )
    if not request.app.state.accounts.finish_link(
        user["token_hash"], name, secret
    ):
        raise HTTPException(400, "Session expired; start a new Last.fm link")
    # Remove the provider token and state from the address bar immediately.
    return RedirectResponse("/", status_code=303)
