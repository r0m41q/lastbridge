import hmac
import secrets
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from lastbridge.accounts import SESSION_SECONDS
from lastbridge.catalog import CatalogError
from lastbridge.lastfm import AUTH_VALUE, LastfmError
from lastbridge.ui import badge, document, heading, icon

router = APIRouter()
COOKIE = "lastbridge_session"
LOGIN_CSRF = "lastbridge_login_csrf"


def page(
    title: str, body: str, status: int = 200, *, user=None
) -> HTMLResponse:
    return HTMLResponse(
        document(title, body, user),
        status_code=status,
    )


@router.get("/assets/style.css", include_in_schema=False)
async def stylesheet():
    return FileResponse(
        Path(__file__).with_name("static") / "style.css", media_type="text/css"
    )


def csrf_field(value: str) -> str:
    return f'<input type="hidden" name="csrf" value="{escape(value)}">'


def form(action: str, csrf: str, label: str) -> str:
    kind = "danger" if action.endswith("/delete") else "secondary"
    return (
        f'<form class="{kind}" method="post" action="{action}">'
        f"{csrf_field(csrf)}"
        f'<button type="submit">{escape(label)}</button></form>'
    )


async def parameters(
    request: Request, multiple=frozenset(), *, field_limit=128, body_limit=None
) -> dict:
    pairs = list(request.query_params.multi_items())
    if request.method == "POST":
        if request.headers.get("content-type", "").split(";")[0] != (
            "application/x-www-form-urlencoded"
        ):
            raise HTTPException(415, "Use a URL-encoded form")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > (
                body_limit
                if body_limit is not None
                else (16384 if multiple else 4096)
            ):
                raise HTTPException(413, "Form too large")
        try:
            pairs.extend(
                parse_qsl(
                    body.decode(),
                    keep_blank_values=True,
                    max_num_fields=field_limit if multiple else 32,
                )
            )
        except ValueError, UnicodeError:
            raise HTTPException(400, "Invalid form") from None
    result = {}
    if multiple and len(pairs) > field_limit:
        raise HTTPException(400, "Too many parameters")
    for key, value in pairs:
        if (key in result and key not in multiple) or len(value) > 2048:
            raise HTTPException(400, "Duplicate or oversized parameter")
        if key in multiple:
            result.setdefault(key, []).append(value)
        else:
            result[key] = value
    return result


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


@router.get("/login")
async def login_page(request: Request):
    csrf = secrets.token_urlsafe(32)
    response = page(
        "Sign in",
        '<section class="auth-form login-panel"><h1>Sign in</h1>'
        '<form method="post" action="/login">'
        + csrf_field(csrf)
        + '<label>Username <input name="username" required maxlength="64" '
        'autocomplete="username" autocapitalize="none" '
        'spellcheck="false" placeholder="Your username"></label>'
        '<label>Password <input type="password" name="password" required '
        'maxlength="256" autocomplete="current-password" '
        'placeholder="Your browser password"></label>'
        '<button type="submit">Sign in</button></form>'
        '<p class="auth-note">Need an account? Your server administrator '
        "can create one for you. Music apps use a separate client password."
        "</p></section>",
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
    downloads = request.app.state.library.downloads(user["id"])
    saved = sum(item["state"] == "complete" for item in downloads)
    pending = len(downloads) - saved
    playlists = request.app.state.playlists.local(user)
    body = '<div class="dashboard"><div class="stats">'
    for label, value, symbol in (
        ("Saved recordings", saved, "library"),
        ("Download jobs", pending, "sync"),
        ("Your playlists", len(playlists), "list"),
    ):
        body += (
            f'<div class="stat"><p class="eyebrow">{label}</p>'
            f"<strong>{value}</strong>{icon(symbol)}</div>"
        )
    body += (
        "</div><section>"
        + heading(
            "", "Connections"
        )
        + '<div class="columns"><section class="panel" id="lastfm-connection">'
    )
    body += (
        '<div class="card-top"><span class="card-icon">'
        + icon("sync")
        + "</span>"
        + badge(
            "Connected" if linked else "Connection needed",
            "good" if linked else "warm",
        )
        + "</div><h3>Your listening, connected (Last.fm).</h3>"
    )
    if linked:
        body += (
            f"<p>Last.fm: <strong>{escape(user['lastfm_name'])}</strong> "
            "(linked). Your listening helps shape your next discovery.</p>"
        )
    elif user["link_status"] == "relink":
        body += (
            "<p>Last.fm authorization was revoked. Link again to continue.</p>"
        )
    else:
        body += "<p>Link Last.fm before connecting a music client.</p>"
    if request.app.state.lastfm.configured:
        body += (
            '<div class="actions">'
            + form(
                "/lastfm/link",
                user["csrf"],
                "Relink Last.fm" if linked else "Link Last.fm",
            )
            + "</div>"
        )
    else:
        body += (
            '<p class="notice">Ask the administrator to configure '
            "Last.fm keys.</p>"
        )
    body += '</section><section class="panel"><div class="card-top">'
    body += '<span class="card-icon">' + icon("key") + "</span>"
    body += badge("Ready to connect" if linked else "Link Last.fm first")
    body += "</div><h3>Music clients</h3>"
    body += (
        "<p>Generate a client password to connect your music app.</p>"
    )
    if linked:
        body += (
            "<details><summary>How to connect</summary><p>Use your username "
            "and a generated client password in music apps. The same value "
            "can be used as an OpenSubsonic API key. Generating a new one "
            "disconnects clients using the old value.</p></details>"
            '<div class="actions">'
        )
        body += (
            form(
                "/account/client-password",
                user["csrf"],
                "Generate replacement client password"
                if user["client_secret"]
                else "Generate client password",
            )
            + "</div>"
        )
    body += "</section></div></section>"
    if user["admin"]:
        body += (
            '<section class="panel admin-panel">'
            + heading(
                "", "Create user",
            )
            + '<form method="post" action="/admin/users">'
            + csrf_field(user["csrf"])
            + '<label>Username <input name="username" required maxlength="64" '
            'autocomplete="off" autocapitalize="none" spellcheck="false" '
            'placeholder="e.g. alex"></label><label>Initial browser password '
            '<input type="password" name="password" minlength="8" '
            'maxlength="256" required autocomplete="new-password" '
            'placeholder="At least 8 characters"></label>'
            '<button type="submit">Create user</button></form></section>'
        )
    body += form("/logout", user["csrf"], "Sign out") + "</div>"
    return page("Your account", body, user=user)


@router.get("/library")
async def library_page(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    library = request.app.state.library
    downloads = library.downloads(user["id"])
    body = '<div class="dashboard"><section class="panel">'
    body += heading("", "Downloads")
    body += (
        "<p>Heart a song in your music app to keep it here. "
        "Your favourites and downloaded playlists "
        "share the same collection.</p>"
    )
    if downloads:
        body += (
            "<div class='table-scroll'><table><tr><th scope=col>Track</th>"
            "<th scope=col>Download</th><th scope=col>Attempts</th></tr>"
        )
        for item in downloads:
            state = "saved" if item["state"] == "complete" else item["state"]
            body += (
                f"<tr><td>{escape(item['title'])}</td><td>"
                + badge(state, "good" if state == "saved" else "warm")
                + f"{': ' + escape(item['error']) if item['error'] else ''}"
                f"</td><td>{item['attempts']}</td></tr>"
            )
        body += "</table></div>"
        body += form(
            "/library/retry",
            user["csrf"],
            "Retry my pending and failed downloads",
        )
    else:
        body += (
            "<p>No saved recordings yet. Heart a song or album "
            'in your music app or <a href="/playlists">'
            "keep a playlist</a>.</p>"
        )
    body += (
        "<details><summary>How saved music works</summary><p>Song hearts "
        "save a recording; album hearts save available album tracks. "
        "Artist hearts are bookmarks. "
        "Favorites stay saved if a download fails. "
        "Unheart in your music app to release your favorite pin; other users "
        "and downloaded playlists keep theirs.</p><p>"
        '<a href="/playlists">Manage playlist downloads</a>.</p>'
        "</details></section></div>"
    )
    return page("Saved library", body, user=user)


@router.post("/library/retry")
async def retry_downloads(request: Request):
    user, _ = await protected_form(request)
    if user["link_status"] != "linked":
        raise HTTPException(403, "Link Last.fm first")
    request.app.state.library.retry(user["id"])
    return RedirectResponse("/library", status_code=303)


@router.get("/lastfm/sync")
async def sync_page(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    job, counts, items, reports = request.app.state.sync.snapshot(user["id"])
    body = '<div class="dashboard"><section class="panel">'
    if user["link_status"] != "linked":
        body += (
            "<p>Relink Last.fm on your account page to resume delivery.</p>"
        )
    body += (
        "<h2>Loved-track import</h2><p>Starts automatically after linking. "
        "Confident matches become favorites and queue permanent downloads. "
        "Local unlikes stay removed on refresh. Pausing stops new import "
        "work; downloads already queued can finish. Reload for progress.</p>"
    )
    if job:
        total_pages = (
            str(job["total_pages"])
            if job["total_pages"] is not None
            else "unknown"
        )
        body += (
            f"<p>Account: {escape(job['target'])}. "
            f"Status: {escape(job['state'])}. "
            f"Next page: {job['next_page']}; total pages: "
            f"{total_pages}. "
            f"Fetched rows: {job['fetched']}.</p>"
        )
        if job["error"]:
            body += f"<p>{escape(job['error'])}</p>"
        body += (
            "<ul>"
            + "".join(
                f"<li>{escape(state)}: {count}</li>"
                for state, count in sorted(counts.items())
            )
            + "</ul>"
        )
        for action, label in (
            ("pause", "Pause import"),
            ("resume", "Resume import"),
            ("refresh", "Refresh Last.fm loves"),
            ("retry-matches", "Retry unmatched recordings"),
        ):
            body += form("/lastfm/sync/" + action, user["csrf"], label)
        body += "<p>Up to 200 unmatched or locally removed recordings:</p><ul>"
        for item in items:
            body += (
                f"<li>{escape(item['artist'])} — {escape(item['title'])}: "
                f"{escape(item['state'])}; {escape(item['error'] or '')}</li>"
            )
        body += "</ul>"
    else:
        body += "<p>No import queued. Link Last.fm to start.</p>"
    body += (
        '</section><section class="panel"><h2>Scrobbles</h2>'
        "<p>Only completed-play reports count. "
        "Streaming and downloading do not count. Configure your client "
        "to submit "
        "after half the track or four minutes. Avoid enabling another Last.fm "
        "scrobbler for the same playback.</p>"
        "<p>Uncertain means Last.fm may already have accepted a report. "
        "Automatic retries stop for these reports. Check your Last.fm history "
        "before retrying; a manual retry may create a duplicate.</p>"
        "<p>Latest 100 reports:</p><div class='table-scroll'><table><tr>"
        "<th scope=col>Track</th><th scope=col>Account</th>"
        "<th scope=col>Start (UTC Unix seconds)</th><th scope=col>Status</th>"
        "<th scope=col>Attempts</th></tr>"
    )
    for report in reports:
        body += (
            f"<tr><td>{escape(report['title'])}</td>"
            f"<td>{escape(report['target'])}</td>"
            f"<td>{report['timestamp']}</td><td>{escape(report['state'])}: "
            f"{escape(report['error'] or '')}</td>"
            f"<td>{report['attempts']}</td></tr>"
        )
    body += "</table></div>"
    body += form(
        "/lastfm/sync/retry-scrobbles",
        user["csrf"],
        "Retry failed or blocked reports for my linked account",
    )
    body += form(
        "/lastfm/sync/retry-uncertain",
        user["csrf"],
        "Retry uncertain reports — may duplicate Last.fm history",
    )
    body += "</section></div>"
    return page("Last.fm sync", body, user=user)


@router.post("/lastfm/sync/{action}")
async def sync_action(request: Request, action: str):
    user, _ = await protected_form(request)
    if action != "pause" and user["link_status"] != "linked":
        raise HTTPException(403, "Link Last.fm first")
    try:
        request.app.state.sync.control(user["id"], action)
    except ValueError:
        raise HTTPException(400, "Unknown sync action") from None
    return RedirectResponse("/lastfm/sync", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    await protected_form(request)
    request.app.state.accounts.logout(request.cookies.get(COOKIE, ""))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@router.get("/mixes")
async def mixes_page(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    mixes = request.app.state.mixes
    status = mixes.status(user["id"])
    items = mixes.playlists(user)
    body = (
        '<div class="dashboard"><section>'
        + heading("", "Mixes",)
        + '<div class="mix-grid">'
    )
    descriptions = (
        ("comfort", "Favourites and top tracks."),
        ("discover", "Last.fm similar-track recommendations."),
        ("again", "Recently played tracks."),
        ("loved", "Imported Last.fm loved tracks."),
    )
    for number, (item, (kind, description)) in enumerate(
        zip(items, descriptions, strict=True), 1
    ):
        body += (
            '<article class="mix-card">'
            f'<div class="mix-art {kind}" aria-hidden="true">'
            f'<span class="mix-number">VOL. 0{number}</span>'
            f"<strong>{escape(item['name'])}</strong></div>"
            f"<h3>{escape(item['name'])}</h3>"
            f'<p class="mix-description">{description}</p>'
            f"<p>{item['songCount']} tracks · "
            "Open in your music app</p></article>"
        )
    body += '</div></section><section class="panel">'
    body += '<div class="card-top"><span class="card-icon">'
    body += icon("sync") + "</span>"
    body += (
        badge(
            "Refreshing"
            if status and status["running"]
            else "Your refresh schedule",
            "good",
        )
        + "</div>"
    )
    body += "<h2>Refresh schedule</h2>"
    body += (
        f"<p>Automatic refresh: every {mixes.refresh_interval // 3600} hours "
        "after a successful refresh, including manual refreshes. Pauses after "
        f"{mixes.inactive_ttl // 86400} days without activity. Returning "
        "queues overdue work in the background; "
        "existing mixes stay usable.</p>"
    )
    if status:
        from lastbridge.mixes import date

        state = (
            "refreshing" if status["running"] else "waiting for next refresh"
        )
        last = (
            escape(date(status["updated"])) if status["updated"] else "Not yet"
        )
        next_time = (
            escape(date(status["next_attempt"]))
            if status["next_attempt"]
            else "Queued"
        )
        body += (
            '<div class="schedule-row"><div><p class="eyebrow">Status</p>'
            f"<strong>Status: {state}.</strong></div>"
            '<div><p class="eyebrow">Last successful refresh</p>'
            f"<strong>{last}</strong></div>"
            '<div><p class="eyebrow">Next refresh or retry eligible at:</p>'
            f"<strong>{next_time} (UTC)</strong></div></div>"
        )
        if status["error"]:
            body += f'<p class="notice">{escape(status["error"])}</p>'
        body += '<div class="actions">'
        body += form("/mixes/refresh", user["csrf"], "Refresh my mixes")
        body += '<span class="muted">Manual refresh is limited to once '
        body += "every five minutes.</span></div>"
    else:
        body += (
            '<p class="notice">Link Last.fm to start generating '
            "your mixes.</p>"
        )
    body += (
        "<details><summary>What goes into your mixes?</summary>"
        "<p>Comfort uses favorites and six-month top tracks. Discover uses "
        "similar tracks outside your known listening sample; new profiles "
        "get chart suggestions. Listen Again uses recent completed listening. "
        "Loved Tracks shows matched Last.fm loves still saved locally.</p>"
        "<p>Generated mixes have up to 30 tracks and keep their previous "
        "snapshot during outages. Initial generation may take up to three "
        "minutes. Unavailable or ambiguous matches are omitted. Loved Tracks "
        "follows the import and local hearts immediately. "
        "Reload for progress.</p>"
        "<p>Mix generation stores metadata; it does not save audio. "
        "Heart a song to keep it downloaded.</p></details></section></div>"
    )
    return page("Personal mixes", body, user=user)


@router.get("/playlists")
async def playlists_page(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user["link_status"] not in ("linked", "relink"):
        raise HTTPException(403, "Link Last.fm first")
    playlists = request.app.state.playlists
    local = playlists.local(user)
    downloaded = playlists.downloaded(user)
    body = '<div class="dashboard"><section class="panel">'
    body += heading("", "Create playlist")
    body += (
        "<p>Edit tracks and their order in your music app.</p>"
        '<form class="inline-form" method="post" '
        'action="/playlists/create">'
        + csrf_field(user["csrf"])
        + '<label>Name <input name="name" required maxlength="200" '
        'placeholder="Playlist name"></label>'
        '<button type="submit">Create empty playlist</button></form></section>'
        "<section>"
    )
    body += heading(
        "", "Your playlists", f"{len(local)} playlists"
    )
    if not local:
        body += (
            '<div class="panel empty-state"><span class="card-icon">'
            + icon("list")
            + "</span><h3>No playlists yet.</h3>"
            "<p>Create one above, or make an editable copy of a mix below.</p>"
            "</div>"
        )
    for item in local:
        item_id = item["id"]
        kept = item_id in downloaded
        body += (
            '<article class="panel playlist-row"><span class="card-icon">'
            + icon("list")
            + '</span><div class="playlist-info">'
            f"<h3>{escape(item['name'])}</h3><p>{item['songCount']} tracks · "
            + ("Kept downloaded." if kept else "Metadata only.")
            + "</p></div>"
            '<div class="actions">'
            + form(
                f"/playlists/{item_id}/release"
                if kept
                else f"/playlists/{item_id}/keep",
                user["csrf"],
                "Release downloads" if kept else "Keep downloaded",
            )
            + form(
                f"/playlists/{item_id}/delete", user["csrf"], "Delete playlist"
            )
            + "</div></article>"
        )
    body += '</section><section class="panel">'
    body += heading("", "Copy a playlist")
    body += (
        "<p>Copies are independent, editable snapshots. "
        "Later source refreshes do not change the copy.</p>"
    )

    choices = local + request.app.state.mixes.playlists(user)
    radio = request.app.state.radio.playlist(user)
    if radio:
        choices.append(radio)
    if choices:
        body += (
            '<form method="post" action="/playlists/copy">'
            + csrf_field(user["csrf"])
            + '<label>Source <select name="source">'
        )
        for item in choices:
            body += (
                f'<option value="{item["id"]}">{escape(item["name"])}</option>'
            )
        body += (
            '</select></label><label>Copy name <input name="name" '
            'required maxlength="200"></label>'
            '<button type="submit">Create editable copy</button></form>'
        )
    body += (
        '</section><section class="panel"><details>'
        "<summary>About downloads and storage</summary>"
        "<p>Ordinary playlists store references only. Keep downloaded queues "
        "tracks for permanent server storage, including later additions. "
        "This does not download anything onto your phone or add hearts. "
        "Download progress and retry are on the Saved library page.</p>"
        "<p>Releasing a playlist keeps its track list. Audio stays retained "
        "while any favorite or downloaded playlist needs it; otherwise it "
        "returns to the temporary cache. Loaded client queues are unchanged."
        "</p></details></section></div>"
    )
    return page("Playlists", body, user=user)


@router.post("/playlists/{action}")
async def new_playlist(request: Request, action: str):
    user, values = await protected_form(request)
    if user["link_status"] not in ("linked", "relink"):
        raise HTTPException(403, "Link Last.fm first")
    playlists = request.app.state.playlists
    try:
        if action == "create":
            await playlists.create(user, values.get("name"), [])
        elif action == "copy":
            await playlists.copy(
                user, values.get("source", ""), values.get("name")
            )
        else:
            raise HTTPException(404, "Action not found")
    except CatalogError as error:
        raise HTTPException(400, error.message) from None
    return RedirectResponse("/playlists", status_code=303)


@router.post("/playlists/{item_id}/{action}")
async def playlist_action(request: Request, item_id: str, action: str):
    user, _ = await protected_form(request)
    if user["link_status"] not in ("linked", "relink"):
        raise HTTPException(403, "Link Last.fm first")
    playlists = request.app.state.playlists
    try:
        if action in ("keep", "release"):
            await playlists.keep(user, item_id, action == "keep")
        elif action == "delete":
            await playlists.delete(user, item_id)
        else:
            raise HTTPException(404, "Action not found")
    except CatalogError as error:
        raise HTTPException(
            404 if error.code == 70 else 400, error.message
        ) from None
    return RedirectResponse("/playlists", status_code=303)


@router.post("/mixes/refresh")
async def refresh_mixes(request: Request):
    user, _ = await protected_form(request)
    if user["link_status"] != "linked":
        raise HTTPException(403, "Link Last.fm first")
    try:
        request.app.state.mixes.request(user["id"])
    except ValueError as error:
        return page(
            "Refresh not queued",
            f'<p>{escape(str(error))}</p><a href="/mixes">Back to mixes</a>',
            409,
            user=user,
        )
    return RedirectResponse("/mixes", status_code=303)


@router.get("/radio")
async def radio_page(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    body = (
        '<p><a href="/mixes">Personal mixes</a></p>'
        "<p>Choose a song to start radio. Then open the Radio playlist in "
        "your music app. This works when the app has no autoplay control. "
        "Only your latest radio is kept. Changing it leaves an already loaded "
        "queue alone. Generation may take up to 20 seconds.</p>"
    )
    radio = request.app.state.radio.playlist(user)
    if radio:
        body += f"<p>{escape(radio['name'])}: {radio['songCount']} tracks.</p>"
    songs = request.app.state.library.collection(user["id"])["song"][:100]
    for playlist in request.app.state.mixes.playlists(user):
        songs += request.app.state.mixes.playlists(user, playlist["id"])[
            "entry"
        ]
    choices = {s["id"]: s for s in songs}
    if choices and user["link_status"] in ("linked", "relink"):
        body += '<form method="post" action="/radio">' + csrf_field(
            user["csrf"]
        )
        body += '<label>Starting song <select name="id">'
        for song in list(choices.values())[:100]:
            label = song.get("artist", "") + " — " + song["title"]
            body += (
                f'<option value="{escape(song["id"])}">'
                f"{escape(label)}</option>"
            )
        body += (
            '</select></label><button type="submit">'
            "Start radio</button></form>"
        )
    else:
        body += (
            "<p>Link Last.fm and wait for personal mixes, "
            "or save a song first.</p>"
        )
    return page("Radio", body, user=user)


@router.post("/radio")
async def radio_action(request: Request):
    user, values = await protected_form(request)
    if user["link_status"] not in ("linked", "relink"):
        raise HTTPException(403, "Link Last.fm first")
    try:
        await request.app.state.radio.songs(user, values.get("id", ""), 50)
    except CatalogError as failure:
        return page(
            "Radio unavailable",
            f"<p>{escape(failure.message)}</p>"
            '<a href="/radio">Back to radio</a>',
            503,
            user=user,
        )
    return RedirectResponse("/radio", status_code=303)


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
        return page(
            "Account not created",
            f"<p>{escape(str(error))}</p>",
            400,
            user=user,
        )
    return page(
        "User created", '<a href="/">Back to your account</a>', user=user
    )


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
        user=user,
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
        not AUTH_VALUE.fullmatch(token)
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
            user=user,
        )
    if not request.app.state.accounts.finish_link(
        user["token_hash"], name, secret
    ):
        raise HTTPException(400, "Session expired; start a new Last.fm link")
    request.app.state.sync.linked(user["id"])
    request.app.state.mixes.linked(user["id"])
    # Remove the provider token and state from the address bar immediately.
    return RedirectResponse("/", status_code=303)
