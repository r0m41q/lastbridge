from lastbridge.transport.subsonic.serialization import reply


def authenticate(request, values):
    token_auth = "t" in values or "s" in values
    mechanisms = sum(("p" in values, token_auth, "apiKey" in values))
    if mechanisms > 1 or ("apiKey" in values and "u" in values):
        return reply(values, 43, "Conflicting authentication mechanisms")
    if mechanisms == 0 or ("apiKey" not in values and not values.get("u")):
        return reply(values, 40, "Wrong username or client password")
    user = request.app.state.accounts.client_user(values)
    if user is None:
        code = 44 if "apiKey" in values else 40
        return reply(values, code, "Wrong username or client password")
    if not values.get("v") or not values.get("c"):
        return reply(values, 10, "Required parameters: v and c")
    if user["link_status"] not in ("linked", "relink"):
        return reply(
            values, 50, "Link Last.fm in your Lastbridge browser account"
        )
    if values.get("f", "xml") not in ("xml", "json"):
        return reply(values, 0, "Supported formats: XML and JSON")
    return user
