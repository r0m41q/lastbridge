import hmac

from fastapi import HTTPException, Request

from lastbridge.transport.http.parameters import parameters

COOKIE = "lastbridge_session"
LOGIN_CSRF = "lastbridge_login_csrf"


def current_user(request: Request):
    user = request.app.state.accounts.session(request.cookies.get(COOKIE, ""))
    if user is not None and request.method == "GET":
        request.app.state.mixes.touch(user["id"])
    return user


async def protected_form(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Sign in through /login")
    values = await parameters(request)
    check_csrf(request, values.get("csrf", ""), user["csrf"])
    request.app.state.mixes.touch(user["id"])
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
