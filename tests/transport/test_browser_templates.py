"""Template safety and explicit page state survive presentation changes."""

from html.parser import HTMLParser

import pytest

from lastbridge.transport.browser.rendering import Page, document, environment

USER = {"username": "listener", "link_status": "linked", "admin": False}


class Markup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


@pytest.mark.parametrize("layout", ["panel", "dashboard", "auth"])
def test_page_state_controls_shell_independently_of_title_and_body(layout):
    html = document(
        "message.html",
        Page("Sign in", "/mixes", layout),
        user=USER,
        message='class="dashboard" <script>untrusted</script>',
        back_url=None,
        secret=None,
    )
    tags = Markup(html).tags
    assert any(
        tag == "a"
        and attrs.get("href") == "/mixes"
        and attrs.get("aria-current") == "page"
        for tag, attrs in tags
    )
    classes = [attrs.get("class", "") for tag, attrs in tags if tag == "div"]
    expected = {
        "panel": "page-content panel",
        "dashboard": "page-content",
        "auth": None,
    }[layout]
    assert [value for value in classes if "page-content" in value] == (
        [expected] if expected else []
    )
    assert not any(tag == "script" for tag, _ in tags)
    assert "&lt;script&gt;" in html


def test_shared_form_escapes_attribute_values_and_labels():
    value = '" autofocus onfocus="alert(1)<&>'
    form = environment.get_template("components/forms.html").module.action(
        "/playlists/example/delete", value, "<script>Delete</script>", True
    )
    tags = Markup(str(form)).tags
    inputs = [attrs for tag, attrs in tags if tag == "input"]
    assert inputs == [{"type": "hidden", "name": "csrf", "value": value}]
    assert not any(tag == "script" for tag, _ in tags)
    assert 'class="danger"' in form


def test_radio_template_escapes_provider_metadata_and_option_values():
    value = '"><script>alert(1)</script>'
    html = document(
        "radio/index.html",
        Page("Radio", "/radio"),
        user=USER | {"csrf": "synthetic-token"},
        radio={"name": value, "songCount": 1},
        choices=[{"id": value, "artist": value, "title": value}],
    )
    tags = Markup(html).tags
    assert not any(tag == "script" for tag, _ in tags)
    assert [attrs["value"] for tag, attrs in tags if tag == "option"] == [
        value
    ]
    assert "&lt;script&gt;" in html
