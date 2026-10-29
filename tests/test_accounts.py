import asyncio
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit
from xml.etree.ElementTree import fromstring

import httpx
import pytest
from cryptography.fernet import Fernet, InvalidToken
from fastapi.testclient import TestClient

from lastbridge.accounts import Accounts, digest
from lastbridge.app import create_app
from lastbridge.config import Settings
from lastbridge.lastfm import Lastfm, LastfmError
from lastbridge.web import COOKIE

PASSWORD = "browser password for tests"
TOKEN = "a" * 32
LASTFM_SESSION = "c" * 32
PARAMS = {"v": "1.16.1", "c": "test", "f": "json"}


def csrf(response) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


def login(client, username="admin", password=PASSWORD):
    response = client.get("/login")
    return client.post(
        "/login",
        data={
            "csrf": csrf(response),
            "username": username,
            "password": password,
        },
        follow_redirects=False,
    )


def start_link(client):
    response = client.post(
        "/lastfm/link",
        data={"csrf": csrf(client.get("/"))},
        follow_redirects=False,
    )
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    callback = query["cb"][0]
    return parse_qs(urlsplit(callback).query)["state"][0]


def finish_link(client, state, token=TOKEN):
    return client.get(
        "/lastfm/callback",
        params={"state": state, "token": token},
        follow_redirects=False,
    )


@pytest.fixture
def service(tmp_path, monkeypatch):
    store = Accounts(tmp_path / "data")
    store.create_user("admin", PASSWORD, bootstrap=True)
    store.create_user("alice", PASSWORD)
    calls = []

    async def exchange(self, client, token):
        calls.append(token)
        return "Lastfm-" + token[0], LASTFM_SESSION

    monkeypatch.setattr(Lastfm, "exchange", exchange)

    async def loved(self, client, name, page):
        return [], 0

    monkeypatch.setattr(Lastfm, "loved", loved)

    async def taste_rows(self, client, method, **parameters):
        return []

    monkeypatch.setattr(Lastfm, "taste_rows", taste_rows)
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            public_url="http://testserver",
            lastfm_api_key="1" * 32,
            lastfm_api_secret="2" * 32,
        )
    )
    with TestClient(app) as client:
        yield client, app, calls


def drop_sync_tables(connection):
    connection.execute("DROP TABLE radio")
    connection.execute("DROP TABLE mixes")
    for table in (
        "playback_starts",
        "scrobbles",
        "love_imports",
        "loved_items",
        "favorite_removals",
        "love_removals",
    ):
        connection.execute(f"DROP TABLE {table}")


def test_migration_restart_encryption_and_recovery(tmp_path):
    store = Accounts(tmp_path)
    store.create_user("admin", PASSWORD, bootstrap=True)
    with pytest.raises(ValueError, match="already complete"):
        store.create_user("second", PASSWORD, bootstrap=True)
    token = store.login("admin", PASSWORD, "test")
    user = store.session(token)
    secret = store.rotate_client_secret(user["id"])
    assert store.finish_link(user["token_hash"], "lastfm-user", LASTFM_SESSION)
    state = store.start_link(user["token_hash"])
    store = Accounts(tmp_path)
    user = store.session(token)
    assert user["link_status"] == "linked"
    assert store.decrypt(user["lastfm_secret"]) == LASTFM_SESSION
    assert store.claim_link(state, TOKEN, user["token_hash"])
    assert store.client_user({"apiKey": secret})["id"] == user["id"]
    database = store.path.read_bytes()
    for value in (PASSWORD, secret, LASTFM_SESSION, token, state):
        assert value.encode() not in database
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "credentials.key").stat().st_mode & 0o777 == 0o600
    with store.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    store.reset_password("admin", "replacement browser password")
    assert store.session(token) is None
    assert store.client_user({"apiKey": secret}) is None
    assert store.login("admin", PASSWORD, "test") is None
    assert store.login("admin", "replacement browser password", "test")
    assert store.user("admin")["link_status"] == "linked"


def test_missing_or_wrong_key_does_not_silently_replace_credentials(tmp_path):
    store = Accounts(tmp_path)
    key = tmp_path / "credentials.key"
    key.unlink()
    with pytest.raises(RuntimeError, match="key missing"):
        Accounts(tmp_path)
    assert not key.exists()
    key.write_bytes(Fernet.generate_key())
    with pytest.raises(InvalidToken):
        Accounts(tmp_path)
    assert store.path.exists()


def test_newer_database_refused(tmp_path):
    store = Accounts(tmp_path)
    with store.transaction() as connection:
        connection.execute("PRAGMA user_version=7")
    with pytest.raises(RuntimeError, match="newer"):
        Accounts(tmp_path)


def test_browser_csrf_rotation_logout_and_admin_boundary(service):
    client, app, _ = service
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.post("/login", data={"csrf": "wrong"}).status_code == 403
    response = login(client)
    assert response.status_code == 303
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    old_token = client.cookies[COOKIE]
    assert login(client).status_code == 303
    assert app.state.accounts.session(old_token) is None
    response = client.get("/")
    assert 'name="password"' in response.text
    assert 'type="submit">Create user' in response.text
    values = {"csrf": csrf(response), "username": "bob", "password": PASSWORD}
    assert (
        client.post(
            "/admin/users",
            data=values,
            headers={"Origin": "https://foreign.invalid"},
        ).status_code
        == 403
    )
    assert client.post("/admin/users", data=values).status_code == 200
    assert not app.state.accounts.user("bob")["admin"]
    assert login(client, "alice").status_code == 303
    values["csrf"] = csrf(client.get("/"))
    assert client.post("/admin/users", data=values).status_code == 403
    assert (
        client.post(
            "/account/client-password", data={"csrf": values["csrf"]}
        ).status_code
        == 403
    )
    token = client.cookies[COOKIE]
    assert (
        client.post(
            "/logout", data={"csrf": values["csrf"]}, follow_redirects=False
        ).status_code
        == 303
    )
    assert app.state.accounts.session(token) is None


def test_link_is_bound_to_session_and_account_and_cannot_replay(service):
    client, app, calls = service
    login(client)
    state = start_link(client)
    admin_cookie = client.cookies[COOKIE]
    login(client, "alice")  # Invalidates the old admin session and its flow.
    assert finish_link(client, state).status_code == 400
    assert not calls
    state = start_link(client)
    alice_cookie = client.cookies[COOKIE]
    second = TestClient(app)
    try:
        login(second)
        assert finish_link(second, state).status_code == 400
    finally:
        second.close()
    assert finish_link(client, state).status_code == 303
    assert calls == [TOKEN]
    assert finish_link(client, state).status_code == 400
    assert calls == [TOKEN]
    assert app.state.accounts.user("alice")["lastfm_name"] == "Lastfm-a"
    assert app.state.accounts.user("admin")["link_status"] == "unlinked"
    assert app.state.accounts.session(admin_cookie) is None
    assert app.state.accounts.session(alice_cookie)
    state = start_link(client)
    assert (
        finish_link(client, state).status_code == 400
    )  # Used token, fresh state.
    assert len(calls) == 1


def test_failed_relink_keeps_previous_session_and_transient_outage(
    service, monkeypatch
):
    client, app, _ = service
    login(client)
    assert finish_link(client, start_link(client)).status_code == 303
    previous = app.state.accounts.user("admin")["lastfm_secret"]
    state = start_link(client)

    async def fail(self, client, token):
        raise LastfmError("Sensitive provider response")

    monkeypatch.setattr(Lastfm, "exchange", fail)
    response = finish_link(client, state, "b" * 32)
    assert response.status_code == 502
    assert "Sensitive" not in response.text
    assert app.state.accounts.user("admin")["lastfm_secret"] == previous
    assert app.state.accounts.user("admin")["link_status"] == "linked"
    assert finish_link(client, state, "b" * 32).status_code == 400
    app.state.accounts.mark_relink(app.state.accounts.user("admin")["id"])
    assert "Link again" in client.get("/").text
    assert app.state.accounts.user("admin")["lastfm_secret"] is None


def test_expired_session_state_and_atomic_callback_claim(service):
    client, app, _ = service
    login(client)
    store = app.state.accounts
    session = store.session(client.cookies[COOKIE])
    state = start_link(client)
    with store.transaction() as connection:
        connection.execute("UPDATE link_flows SET expires=0")
    assert finish_link(client, state).status_code == 400
    state = start_link(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: store.claim_link(
                    state, TOKEN, session["token_hash"]
                ),
                range(2),
            )
        )
    assert sorted(results) == [False, True]
    with store.transaction() as connection:
        connection.execute("UPDATE sessions SET expires=0")
    assert finish_link(client, start_link.__name__).status_code == 401


@pytest.mark.parametrize("mechanism", ["plain", "hex", "token", "apiKey"])
def test_subsonic_authentication_and_rotation(service, mechanism):
    client, app, _ = service
    store = app.state.accounts
    user = store.user("alice")
    secret = store.rotate_client_secret(user["id"])
    if mechanism == "plain":
        auth = {"u": "alice", "p": secret}
    elif mechanism == "hex":
        auth = {"u": "alice", "p": "enc:" + secret.encode().hex()}
    elif mechanism == "token":
        auth = {
            "u": "alice",
            "s": "random-salt",
            "t": hashlib.md5(
                (secret + "random-salt").encode(), usedforsecurity=False
            ).hexdigest(),
        }
    else:
        auth = {"apiKey": secret}
    values = PARAMS | auth
    assert (
        client.get("/rest/ping", params=values).json()["subsonic-response"][
            "error"
        ]["code"]
        == 50
    )
    token = store.login("alice", PASSWORD, "test")
    store.finish_link(digest(token), "alice-lastfm", LASTFM_SESSION)
    for path in ("/rest/ping", "/rest/ping.view"):
        response = client.get(path, params=values)
        assert response.json()["subsonic-response"]["status"] == "ok"
        response = client.post(path, data=values)
        assert response.json()["subsonic-response"]["status"] == "ok"
    xml = client.get("/rest/ping", params=values | {"f": "xml"})
    assert fromstring(xml.content).attrib["status"] == "ok"
    store.mark_relink(user["id"])
    assert (
        client.get("/rest/ping", params=values).json()["subsonic-response"][
            "status"
        ]
        == "ok"
    )
    store.finish_link(digest(token), "alice-lastfm", LASTFM_SESSION)
    store.rotate_client_secret(user["id"])
    assert client.get("/rest/ping", params=values).json()["subsonic-response"][
        "error"
    ]["code"] in (40, 44)


def test_protocol_errors_and_browser_password_not_accepted(service):
    client, app, _ = service
    for auth, expected in (
        ({}, 40),
        ({"u": "admin", "p": PASSWORD}, 40),
        ({"u": "admin", "p": "enc:nothex"}, 40),
        ({"p": "one", "t": "two"}, 43),
        ({"apiKey": "unknown"}, 44),
        ({"apiKey": "unknown", "u": "alice"}, 43),
    ):
        response = client.get("/rest/ping", params=PARAMS | auth)
        assert (
            response.json()["subsonic-response"]["error"]["code"] == expected
        )
    login(client)
    assert (
        client.post(
            "/admin/users",
            content="csrf=a&csrf=b",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).status_code
        == 400
    )
    assert client.post("/login", data={"huge": "x" * 5000}).status_code == 413
    token = app.state.accounts.login("admin", PASSWORD, "test")
    with app.state.accounts.transaction() as connection:
        connection.execute(
            "UPDATE sessions SET expires=0 WHERE token_hash=?",
            (digest(token),),
        )
    assert app.state.accounts.session(token) is None


def test_login_rate_limit_expires_and_is_shared_across_requests(tmp_path):
    store = Accounts(tmp_path)
    store.create_user("alice", PASSWORD)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO login_limits VALUES(?,10,9999999999)",
            ("user:" + digest("alice"),),
        )
    assert store.login("alice", PASSWORD, "test") is None
    with store.transaction() as connection:
        connection.execute("UPDATE login_limits SET expires=0")
    assert store.login("alice", PASSWORD, "test")


def test_lastfm_signature_and_only_valid_session_is_accepted(tmp_path):
    adapter = Lastfm("1" * 32, "2" * 32)
    observed = []

    def respond(request):
        observed.append(request)
        return httpx.Response(
            200,
            json={"session": {"name": "provider-user", "key": LASTFM_SESSION}},
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as client:
            return await adapter.exchange(client, TOKEN)

    assert asyncio.run(run()) == ("provider-user", LASTFM_SESSION)
    values = dict(observed[0].url.params)
    message = (
        "api_key" + "1" * 32 + "methodauth.getSessiontoken" + TOKEN + "2" * 32
    )
    assert (
        values["api_sig"]
        == hashlib.md5(message.encode(), usedforsecurity=False).hexdigest()
    )
    assert observed[0].url.scheme == "https"
    assert "2" * 32 not in str(observed[0].url)


@pytest.mark.parametrize(
    "result", [{"error": 9}, {}, {"session": {"name": "name", "key": ""}}]
)
def test_lastfm_provider_errors_are_safe(tmp_path, result):

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=result)
            )
        ) as client:
            return await Lastfm("1" * 32, "2" * 32).exchange(client, TOKEN)

    with pytest.raises(LastfmError, match="start a new link"):
        asyncio.run(run())


def test_security_headers_secure_cookies_and_no_configuration_leaks(tmp_path):
    app = create_app(
        Settings(
            _env_file=None, data_dir=tmp_path, public_url="https://testserver"
        )
    )
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/login")
        assert "Secure" in response.headers["set-cookie"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "same-origin"
        assert (
            "frame-ancestors 'none'"
            in response.headers["content-security-policy"]
        )
        app.state.accounts.create_user("admin", PASSWORD, bootstrap=True)
        login(client)
        assert (
            client.post(
                "/lastfm/link", data={"csrf": csrf(client.get("/"))}
            ).status_code
            == 503
        )


def test_management_bootstrap_and_reset_use_hidden_prompts(
    tmp_path, monkeypatch
):
    from lastbridge.manage import main

    monkeypatch.setenv("LASTBRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["manage", "bootstrap-admin", "admin"])
    monkeypatch.setattr("getpass.getpass", lambda _: PASSWORD)
    main()
    assert Accounts(tmp_path).user("admin")["admin"]
    monkeypatch.setattr("sys.argv", ["manage", "reset-password", "admin"])
    monkeypatch.setattr(
        "getpass.getpass", lambda _: "replacement browser password"
    )
    main()
    assert Accounts(tmp_path).login(
        "admin", "replacement browser password", "test"
    )


def test_logout_during_provider_exchange_cannot_finish_link(
    service, monkeypatch
):
    client, app, _ = service
    login(client)
    state = start_link(client)
    session_token = client.cookies[COOKIE]

    async def exchange(self, http_client, token):
        app.state.accounts.logout(session_token)
        return "lastfm-user", LASTFM_SESSION

    monkeypatch.setattr(Lastfm, "exchange", exchange)
    assert finish_link(client, state).status_code == 400
    assert app.state.accounts.user("admin")["link_status"] == "unlinked"


def test_account_page_escapes_provider_text_and_hides_other_user(service):
    client, app, _ = service
    login(client, "alice")
    store = app.state.accounts
    store.finish_link(
        digest(client.cookies[COOKIE]), "<script>user</script>", LASTFM_SESSION
    )
    response = client.get("/")
    assert "&lt;script&gt;user&lt;/script&gt;" in response.text
    assert "<script>" not in response.text
    assert "Create user" not in response.text
    assert LASTFM_SESSION not in response.text
    assert "admin" not in response.text


def test_lastfm_network_error_does_not_expose_request_secrets(tmp_path):

    def fail(request):
        raise httpx.ReadTimeout(f"secret URL: {request.url}")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fail)
        ) as client:
            return await Lastfm("1" * 32, "2" * 32).exchange(client, TOKEN)

    with pytest.raises(LastfmError) as error:
        asyncio.run(run())
    assert TOKEN not in str(error.value)
    assert "secret URL" not in str(error.value)


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.org/path",
        "https://u:p@example.org",
        "file:///tmp",
        "https://example.org?token=secret",
    ],
)
def test_public_origin_rejects_credentials_and_non_origin_urls(origin):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, public_url=origin)


@pytest.mark.parametrize(
    "key,secret",
    [
        ("sensitive-invalid-key", "2" * 32),
        ("1" * 32, "sensitive-invalid-secret"),
        ("1" * 32, ""),
        ("", "2" * 32),
    ],
)
def test_invalid_environment_credentials_are_rejected_safely(key, secret):
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, lastfm_api_key=key, lastfm_api_secret=secret)
    for value in (key, secret):
        if value:
            assert value not in str(error.value)


def test_empty_compose_credentials_are_optional():
    settings = Settings(
        _env_file=None, lastfm_api_key="", lastfm_api_secret=""
    )
    assert not Lastfm(
        settings.lastfm_api_key.get_secret_value(),
        settings.lastfm_api_secret.get_secret_value(),
    ).configured


def test_environment_credentials_enable_linking_and_are_redacted(
    tmp_path, monkeypatch
):
    key, secret = "a" * 32, "b" * 32
    monkeypatch.setenv("LASTBRIDGE_LASTFM_API_KEY", key)
    monkeypatch.setenv("LASTBRIDGE_LASTFM_API_SECRET", secret)
    settings = Settings(_env_file=None, data_dir=tmp_path)
    assert settings.lastfm_api_key.get_secret_value() == key
    assert settings.lastfm_api_secret.get_secret_value() == secret
    for value in (key, secret):
        assert value not in repr(settings)
        assert value not in settings.model_dump_json()
    app = create_app(settings)
    with TestClient(app) as client:
        assert app.state.lastfm.configured
        assert app.state.lastfm.api_key == key
        assert app.state.lastfm.api_secret == secret
        app.state.accounts.create_user("admin", PASSWORD, bootstrap=True)
        login(client)
        response = client.get("/")
        assert "Link Last.fm</button>" in response.text
        assert "configure Last.fm keys" not in response.text
        assert key not in response.text
        assert secret not in response.text


def test_dotenv_credentials_are_loaded_locally(tmp_path, monkeypatch):
    monkeypatch.delenv("LASTBRIDGE_LASTFM_API_KEY", raising=False)
    monkeypatch.delenv("LASTBRIDGE_LASTFM_API_SECRET", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "LASTBRIDGE_LASTFM_API_KEY=" + "c" * 32 + "\n"
        "LASTBRIDGE_LASTFM_API_SECRET=" + "d" * 32 + "\n"
    )
    settings = Settings(_env_file=dotenv)
    assert settings.lastfm_api_key.get_secret_value() == "c" * 32
    assert settings.lastfm_api_secret.get_secret_value() == "d" * 32


def test_browser_form_origin_and_referrer_policy(service):
    client, _, _ = service
    response = client.get("/login")
    # no-referrer makes real browser navigation POSTs send Origin: null.
    # Preserve the origin for local forms; suppress external referrers instead.
    assert response.headers["referrer-policy"] == "same-origin"
    values = {
        "csrf": csrf(response),
        "username": "admin",
        "password": PASSWORD,
    }
    for origin in ("null", "http://foreign.invalid", "http://localhost:5274"):
        assert (
            client.post(
                "/login",
                data=values,
                headers={"Origin": origin},
                follow_redirects=False,
            ).status_code
            == 403
        )
    assert (
        client.post(
            "/login",
            data=values,
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        ).status_code
        == 303
    )


def test_opaque_non_hex_lastfm_token_is_exchanged_once(service):
    client, app, calls = service
    login(client)
    state = start_link(client)
    token = "Synthetic_token-WithNonHexChars9"
    assert len(token) == 32
    assert finish_link(client, state, token).status_code == 303
    assert calls == [token]
    assert app.state.accounts.user("admin")["link_status"] == "linked"
    assert finish_link(client, state, token).status_code == 400
    assert calls == [token]


@pytest.mark.parametrize(
    "token", ["", "has whitespace", "bad\nvalue", "кирилиця"]
)
def test_invalid_callback_token_does_not_consume_flow(service, token):
    client, _, calls = service
    login(client)
    state = start_link(client)
    assert finish_link(client, state, token).status_code == 400
    assert not calls
    assert finish_link(client, state).status_code == 303


def test_opaque_non_hex_session_key_is_accepted_without_exposure():
    key = "Synthetic_session-Key_WithChars9"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={"session": {"name": "provider-user", "key": key}},
                )
            )
        ) as client:
            return await Lastfm("1" * 32, "2" * 32).exchange(client, TOKEN)

    assert asyncio.run(run()) == ("provider-user", key)
