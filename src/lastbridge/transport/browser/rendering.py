"""Namespaced package templates with explicit page state and autoescaping."""

from dataclasses import dataclass
from typing import Literal

from fastapi.responses import HTMLResponse
from jinja2 import (
    ChoiceLoader,
    Environment,
    PackageLoader,
    PrefixLoader,
    StrictUndefined,
    select_autoescape,
)

FEATURES = (
    "accounts",
    "administration",
    "library",
    "lastfm_sync",
    "discovery",
    "playlists",
    "radio",
)
NAV = (
    ("/", "Overview", "home"),
    ("/mixes", "Your mixes", "mix"),
    ("/library", "Saved library", "library"),
    ("/playlists", "Playlists", "list"),
    ("/radio", "Radio", "radio"),
    ("/lastfm/sync", "Last.fm sync", "sync"),
)
environment = Environment(
    loader=ChoiceLoader(
        [
            PackageLoader("lastbridge.transport.browser"),
            PrefixLoader(
                {
                    name: PackageLoader(
                        f"lastbridge.features.{name}.adapters.browser"
                    )
                    for name in FEATURES
                }
            ),
        ]
    ),
    autoescape=select_autoescape(("html",), default_for_string=True),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass(frozen=True)
class Page:
    title: str
    section: str | None = None
    layout: Literal["panel", "dashboard", "auth"] = "panel"


def document(template: str, page: Page, *, user=None, **context) -> str:
    navigation = (
        NAV
        if user is not None and user["link_status"] in ("linked", "relink")
        else NAV[:1]
    )
    if user is not None and user["admin"]:
        if user["link_status"] not in ("linked", "relink"):
            navigation = tuple(
                item for item in NAV if item[0] in ("/", "/playlists")
            )
        navigation = (*navigation, ("/admin", "Administration", "key"))
    return environment.get_template(template).render(
        page=page, user=user, navigation=navigation, **context
    )


def render(
    template: str, page: Page, status_code: int = 200, *, user=None, **context
) -> HTMLResponse:
    return HTMLResponse(
        document(template, page, user=user, **context), status_code=status_code
    )


def message(
    page: Page,
    text="",
    status=200,
    *,
    user=None,
    back_url=None,
    back_label="",
    secret=None,
):
    return render(
        "message.html",
        page,
        status,
        user=user,
        message=text,
        back_url=back_url,
        back_label=back_label,
        secret=secret,
    )
