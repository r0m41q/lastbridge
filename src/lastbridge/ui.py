"""Temporary trusted-body API; HTTP routes use named templates."""

from markupsafe import Markup

from lastbridge.transport.browser.rendering import Page
from lastbridge.transport.browser.rendering import document as render_document


def document(title, body, user=None):
    return render_document(
        "compatibility.html", Page(title), user=user, body=Markup(body)
    )
