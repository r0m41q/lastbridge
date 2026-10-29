from html.parser import HTMLParser

import pytest
import test_accounts
import test_sync
from test_accounts import PASSWORD, csrf, login

from lastbridge.accounts import Accounts, validate_account
from lastbridge.ui import document

service = test_accounts.service
sync_service = test_sync.sync_service


class Markup(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_local_styles_load_under_csp_without_script_permissions(service):
    client = service[0]
    response = client.get("/login")
    assert response.status_code == 200
    tags = Markup(response.text).tags
    assert ("link", {"rel": "stylesheet", "href": "/assets/style.css"}) in tags
    assert not any(tag in {"script", "style"} for tag, _ in tags)
    assert all(not any(k.startswith("on") for k in attrs) for _, attrs in tags)
    policy = response.headers["Content-Security-Policy"]
    assert "default-src 'none'" in policy
    assert "style-src 'self'" in policy
    assert "unsafe-inline" not in policy
    assert "frame-ancestors 'none'" in policy
    css = client.get("/assets/style.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert "prefers-reduced-motion" in css.text
    assert "prefers-color-scheme: light" in css.text
    assert "@import" not in css.text and "url(" not in css.text
    assert client.get("/assets/../accounts.py").status_code == 404


def test_navigation_is_authenticated_and_admin_form_is_role_scoped(
    sync_service,
):
    client = sync_service[0]
    assert "Main navigation" not in client.get("/login").text
    login(client)
    response = client.get("/")
    assert "Main navigation" in response.text
    assert "Skip to content" in response.text
    assert 'minlength="8"' in response.text
    assert "Create user" in response.text
    login(client, "alice")
    response = client.get("/")
    assert "Create user" not in response.text
    assert "Main navigation" in response.text
    assert "Administrator" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/mixes",
        "/library",
        "/playlists",
        "/radio",
        "/lastfm/sync",
    ],
)
def test_pages_have_accessible_structure_and_current_navigation(
    sync_service, path
):
    client = sync_service[0]
    login(client)
    response = client.get(path)
    assert response.status_code == 200
    tags = Markup(response.text).tags
    assert len([tag for tag, _ in tags if tag == "h1"]) == 1
    assert ("main", {"id": "main", "tabindex": "-1"}) in tags
    assert any(
        tag == "a"
        and attrs.get("href") == path
        and attrs.get("aria-current") == "page"
        for tag, attrs in tags
    )
    for tag, attrs in tags:
        if tag == "form" and attrs.get("method") == "post":
            assert attrs["action"].startswith("/")
    if any(tag == "form" for tag, _ in tags):
        assert 'name="csrf"' in response.text


def test_user_values_are_escaped_in_shared_shell():
    html = document(
        "Danger <title>",
        "<p>Trusted body</p>",
        {
            "username": "<script>alert(1)</script>",
            "admin": False,
            "link_status": "linked",
        },
    )
    assert "<script>" not in html
    assert "Danger &lt;title&gt;" in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize("length", [7, 257])
def test_password_bounds_reject_outside_eight_to_256(length):
    with pytest.raises(ValueError, match="8–256"):
        validate_account("listener", "x" * length)


@pytest.mark.parametrize("length", [8, 256])
def test_password_bounds_accept_eight_and_256(length):
    validate_account("listener", "x" * length)


def test_admin_create_and_password_reset_accept_eight_characters(service):
    client, app, _ = service
    login(client)
    token = csrf(client.get("/"))
    assert (
        client.post(
            "/admin/users",
            data={
                "csrf": token,
                "username": "shortpass",
                "password": "1234567",
            },
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/admin/users",
            data={
                "csrf": token,
                "username": "shortpass",
                "password": "12345678",
            },
        ).status_code
        == 200
    )
    assert login(client, "shortpass", "12345678").status_code == 303
    store = Accounts(app.state.accounts.path.parent)
    store.reset_password("shortpass", "abcdefgh")
    assert login(client, "shortpass", "abcdefgh").status_code == 303
    assert login(client, "admin", PASSWORD).status_code == 303
