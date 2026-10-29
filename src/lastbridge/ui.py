from html import escape

NAV = (
    ("/", "Overview", "home"),
    ("/mixes", "Your mixes", "mix"),
    ("/library", "Saved library", "library"),
    ("/playlists", "Playlists", "list"),
    ("/radio", "Radio", "radio"),
    ("/lastfm/sync", "Last.fm sync", "sync"),
)
ICONS = {
    "home": '<path d="m3 10 9-7 9 7v10H3Z"/><path d="M9 20v-7h6v7"/>',
    "mix": '<path d="M4 5h16M4 12h16M4 19h16"/>'
    '<circle cx="9" cy="5" r="2"/><circle cx="15" cy="12" r="2"/>'
    '<circle cx="8" cy="19" r="2"/>',
    "library": '<path d="M4 4v16M9 4v16m5-16 6 15M3 20h18"/>',
    "list": '<path d="M4 5h16M4 12h10M4 19h10m3-6 5 4-5 4Z"/>',
    "radio": '<circle cx="12" cy="12" r="2"/>'
    '<path d="M7 7a7 7 0 0 0 0 10M17 7a7 7 0 0 1 0 10'
    'M4 4a11 11 0 0 0 0 16M20 4a11 11 0 0 1 0 16"/>',
    "sync": '<path d="M20 7h-6m6 0V1M4 17h6m-6 0v6"/>'
    '<path d="M20 7a9 9 0 0 0-16 0m0 10a9 9 0 0 0 16 0"/>',
    "arrow": '<path d="M5 12h14m-6-6 6 6-6 6"/>',
    "key": '<circle cx="8" cy="9" r="5"/>'
    '<path d="m12 13 8 8m-3-3 3-3m-6 0 3-3"/>',
    "users": '<circle cx="9" cy="7" r="3"/>'
    '<path d="M3 21v-4a6 6 0 0 1 12 0v4m1-17a3 3 0 0 1 0 6'
    'm2 4a5 5 0 0 1 3 4v3"/>',
}


def icon(name):
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" '
        'aria-hidden="true">' + ICONS[name] + "</svg>"
    )


def badge(text, tone="neutral"):
    return f'<span class="badge {tone}">{escape(str(text))}</span>'


def heading(label, title, description=""):
    return (
        '<div class="section-heading"><div>'
        f"<h2>{title}</h2></div>"
        + (f'<p class="muted">{description}</p>' if description else "")
        + "</div>"
    )


def document(title, body, user=None):
    section = {
        "Your account": "/",
        "Personal mixes": "/mixes",
        "Saved library": "/library",
        "Playlists": "/playlists",
        "Radio": "/radio",
        "Last.fm sync": "/lastfm/sync",
    }.get(title)
    brand = (
        '<a class="brand" href="/" aria-label="Lastbridge overview">'
        '<span class="brand-mark" aria-hidden="true">'
        "<i></i><i></i><i></i><i></i></span>lastbridge<span>♪</span></a>"
    )
    sidebar = ""
    if user is not None:
        links = "".join(
            f'<a href="{url}"'
            + (' aria-current="page"' if section == url else "")
            + f">{icon(symbol)}<span>{label}</span></a>"
            for url, label, symbol in (
                NAV if user["link_status"] in ("linked", "relink") else NAV[:1]
            )
        )
        sidebar = (
            '<aside class="sidebar">'
            + brand
            + '<nav aria-label="Main navigation">'
            + links
            + "</nav>"
            + '<div class="identity"><span class="avatar">'
            + escape(user["username"][:1].upper())
            + "</span><div><strong>"
            + escape(user["username"])
            + "</strong><small>"
            + ("Administrator" if user["admin"] else "Listener")
            + "</small></div></div></aside>"
        )
    is_auth = title == "Sign in"
    classes = "app-shell" if user is not None else "guest-shell"
    if is_auth:
        content = body
    else:
        content = (
            f'<header class="page-header"><h1>{escape(title)}</h1></header>'
            + (
                '<div class="page-content">'
                if 'class="dashboard"' in body
                else '<div class="page-content panel">'
            )
            + body
            + "</div>"
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="dark light">'
        f"<title>{escape(title)} · Lastbridge</title>"
        '<link rel="stylesheet" href="/assets/style.css"></head>'
        f'<body class="{classes}"><a class="skip-link" href="#main">'
        "Skip to content</a>"
        + sidebar
        + (
            '<header class="guest-brand">' + brand + "</header>"
            if user is None
            else ""
        )
        + '<main id="main" tabindex="-1">'
        + content
        + "</main></body></html>"
    )
