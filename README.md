# Lastbridge

Last.fm discovery and a private saved library through existing OpenSubsonic music
clients. Python service, with YouTube Music as the planned first music provider.

**Current scope: accounts, Last.fm linking, protocol authentication, health
routes, and a developer-only YouTube probe.** The browser account pages work;
the protocol currently implements authenticated `ping` only. Catalog browsing,
audio caching, recommendations, and client playback are not implemented yet.

## Local development

Python 3.14 is the initial supported runtime; verified versions are recorded below.
All local project dependencies and tooling live in `venv/`.

```sh
python3 -m venv venv
venv/bin/python -m pip install uv==0.13.6
UV_PROJECT_ENVIRONMENT=venv venv/bin/uv sync --locked --inexact
cp .env.example .env
venv/bin/lastbridge
```

`--locked` refuses to change the committed dependency lock. `--inexact` preserves
the bootstrapped uv/pip tools in the same environment. To deliberately update
dependencies, run `UV_PROJECT_ENVIRONMENT=venv venv/bin/uv lock`, then sync and
review the lockfile changes. No global uv installation is needed.

```sh
curl --fail http://127.0.0.1:5274/health/live
curl --fail http://127.0.0.1:5274/health/ready
```

Use Ctrl-C to stop. Lifecycle startup creates writable `data/cache/` and
`data/saved/` directories and opens a shared HTTP client; shutdown closes it.
Neither health endpoint calls a music provider or Last.fm. Readiness means local
startup completed; it does not claim that music playback is available or perform
a continuous disk/third-party availability check.

## Accounts and Last.fm linking

Create the first administrator from a trusted local terminal. Passwords use
hidden prompts and must contain 12–256 characters. Usernames use lowercase ASCII
letters, digits, dots, hyphens, and underscores (maximum 64 characters).

```sh
venv/bin/python -m lastbridge.manage bootstrap-admin admin
```

Start the service, open its configured public URL, and sign in. The administrator
can create ordinary users through the account page; there is no public signup.
Bootstrap refuses to run again once any account exists. Administrator recovery:

```sh
venv/bin/python -m lastbridge.manage reset-password admin
```

This trusted local operation revokes the account's browser sessions, pending
links, and client credential, while preserving its Last.fm link. Use the same
data directory as the running service. Browser sessions otherwise expire after
12 hours; signing in replaces the current browser session, and logout revokes it.
Login attempts are limited per username and connecting IP over a one-minute
window. Password hashing runs outside the event loop, with two concurrent
operations at most. Forwarded client-IP headers are not trusted.

Configure your own [Last.fm application](https://www.last.fm/api/account/create)
locally. Use `<public URL>/lastfm/callback` as its callback URL. Put the following
JSON structure in an ignored local file such as `data/lastfm.json`, replacing the
placeholders through your editor, and restrict file permissions to its owner:

```json
{"api_key": "YOUR_32_CHARACTER_API_KEY", "api_secret": "YOUR_32_CHARACTER_SECRET"}
```

Set `LASTBRIDGE_LASTFM_CREDENTIALS_FILE=./data/lastfm.json` locally and
`LASTBRIDGE_PUBLIC_URL` to the exact browser origin, such as
`https://music.example.org`. The public URL has no path prefix, query or fragment;
use that origin consistently for login and linking. Restart after configuration
changes. Last.fm credentials are optional for startup; without them, account
creation/sign-in works and the page explains that linking needs configuration.
A configured unreadable or malformed credential file prevents startup.

Each user signs in through the browser and presses **Link Last.fm**, authorizes
at Last.fm, then returns to the same browser session. The callback uses the
configured public origin, not incoming Host/proxy headers. Local link state is
bound to the session, expires after ten minutes, and is consumed atomically
before exchange. Callback tokens cannot be reused with another flow. Failed
exchange requires a fresh link attempt and preserves any existing working link.
Authorization survives application restarts. Revoked sessions have a persistent
relink state; future authenticated Last.fm operations must mark that state on
provider error 9. No background revocation polling is implemented, and transient
Last.fm failures do not invalidate an existing link.

After linking, generate a **client password** on the account page and copy it
before leaving: it is shown once. Use the Lastbridge username and this value in
music apps. It is separate from the browser password; replacing it revokes the
previous value in every app. It also works as an OpenSubsonic `apiKey` without a
username. Legacy plain/`enc:` password and `md5(client_password + salt)` token
authentication work with this same generated value. The browser password cannot
authenticate protocol calls. Extension discovery and actual client onboarding
come in the next iteration; no extension capabilities are advertised yet.

`/rest/ping` and `/rest/ping.view` support GET and URL-encoded form POST, XML
(default) and JSON (`f=json`). Supply `v` and `c`. Errors use protocol responses:
40/44 for bad credentials, 43 for conflicting mechanisms, 50 for missing/revoked
Last.fm linking, and 0 for unimplemented endpoints. Normal music access requires
a linked account; administration remains accessible before linking.

SQLite schema version 1 is created transactionally on first startup. Newer schema
versions are refused. Browser passwords use salted scrypt; browser session tokens
and callback state are stored as SHA-256 digests. The generated client credential
and Last.fm session key use Fernet authenticated encryption. The database and
`data/credentials.key` use owner-only file permissions. Back up **both** together
with the application stopped; loss of the key makes encrypted credentials
unrecoverable. Startup refuses to replace a missing key or accept a mismatched
one. Encryption protects a database-only copy; anyone who can read the complete
data volume can also read its key. The browser pages return no-store/referrer
headers, escaped text, CSRF-protected forms, and HttpOnly session cookies; cookies
use Secure when the configured public origin is HTTPS.

The implementation follows [Last.fm web authorization](https://www.last.fm/api/webauth)
and the [OpenSubsonic authentication contract](https://opensubsonic.netlify.app/docs/api-reference/).
Tests use a mocked provider exchange; **real Last.fm authorization has not been
tested with live application keys**. For review, bootstrap an administrator,
create a second account, link each to its own Last.fm account, restart the service,
and verify each link remains associated with the correct local account. Catalog
and playback checks belong to later iterations.

## YouTube feasibility probe

The probe runs independently of the service, with explicit network opt-in. Install
its optional dependency group into the same local environment:

```sh
UV_PROJECT_ENVIRONMENT=venv venv/bin/uv sync --locked --group probe --inexact
venv/bin/python -m lastbridge.probe --live --suite \
  --sample-dir data/probe/review --report data/probe/review.json
```

The suite uses Daft Punk — Instant Crush, Joji — PIXELATED KISSES, and OTOY —
Судоми. Samples intentionally contain only ten seconds (about 160 KB for source
AAC near 128 kb/s). This checks acquisition and decoding; it is not a provider
preview restriction. The user confirmed the recordings are correct but considers
quality low. Compare AAC/Opus and actual client support in the playback iteration,
and repeat the probe on the deployment server's connection before deployment.

For samples, install the **FFmpeg and ffprobe binaries** on the host. The resolver
also uses Node.js 22+ by default; Deno is selectable with `--js-runtime deno`.
These binaries and the optional probe packages are not in the service's Docker
image. The service's runtime dependencies are unchanged.

The default player client is `visionos`, which delivered all three test samples
on this machine. `--player-client default` tests yt-dlp's own client selection;
that resolved metadata but the CDN refused all three sample requests here.
Neither setting uses account credentials. This dated result is not a general
availability guarantee.

Without `--sample-dir`, the probe only inspects formats. It prints JSON and exits
nonzero for a failed case; search and inspect/sample stages each have a hard
45-second deadline (`--timeout`, maximum 120 seconds). Killing a timed-out worker
also kills its media subprocesses and removes partial samples. Completed samples
and reports are never overwritten; choose new paths for another run.

Custom recording and diagnostic examples:

```sh
venv/bin/python -m lastbridge.probe --live \
  --artist OTOY --title Судоми --album Судоми --duration 147
venv/bin/python -m lastbridge.probe --live --source-id d8CbKd69KT8
```

Search matching requires catalog audio and matching artist/title, plus album and
duration when supplied. Trailing featured-performer credits are normalized;
live/remix/version qualifiers remain significant. Ambiguous results fail rather
than silently selecting a recording. This is a conservative first matcher, not
universal catalog coverage; explicit source IDs bypass matching for diagnostics.

Samples preserve source AAC/M4A (preferred) or Opus/WebM with stream copying,
without upscaling or transcoding. FFprobe measures the saved sample and FFmpeg
decodes it to check integrity. Reports distinguish resolution from verified
sample decoding and omit signed URLs, headers, and raw upstream errors. Local
reports/audio under `data/` are ignored; keep `uv.lock` committed so dependency
versions remain reproducible.

See the [probe evidence and remaining review checks](docs/youtube-probe.md).

## Configuration

Environment variables override `.env` values. Copy [the example](.env.example)
and keep real credentials out of version control. Unknown `.env` keys are ignored
to allow later configuration additions.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LASTBRIDGE_HOST` | `127.0.0.1` | Local bind address; Compose uses it for host port publishing |
| `LASTBRIDGE_PORT` | `5274` | Local port, or published Compose port |
| `LASTBRIDGE_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `LASTBRIDGE_DATA_DIR` | `./data` | Local persistent directory; Compose uses `/app/data` |
| `LASTBRIDGE_PUBLIC_URL` | `http://127.0.0.1:5274` | Exact browser/callback origin |
| `LASTBRIDGE_LASTFM_CREDENTIALS_FILE` | unset | JSON file with application key/secret; empty means unset |

The supported `lastbridge` entry point disables access logging because Subsonic
requests may carry passwords/tokens in query strings. It logs lifecycle events,
never full requests or settings; HTTP client INFO/DEBUG logs are suppressed.
Invalid configuration exits with code 2 and a generic error without echoing input.
There is no general-purpose sanitizer for arbitrary future application logs;
new code must continue to avoid logging secrets. Reverse proxies need their own
credential-safe logging configuration.

## Docker Compose

```sh
docker compose up --build --wait
curl --fail http://127.0.0.1:5274/health/ready
docker compose logs lastbridge
docker compose down
```

The runtime image runs as UID 10001 with one worker and stores data in the
`lastbridge-data` named volume. It contains the installed application and runtime
dependencies, without pytest, Ruff, uv, or installed pip. Run development checks
locally using `venv/`. The read-only credentials volume can hold the Last.fm
application credential file. A local Compose override can bind an ignored host
file to `/app/credentials/lastfm.json:ro`; make it readable by container UID 10001
and set `LASTBRIDGE_LASTFM_CREDENTIALS_FILE=/app/credentials/lastfm.json` in the
Compose environment. Local host paths and container paths are different.
The build context excludes local environments, `.env`, data, and Git metadata.
Restart after configuration changes and rebuild after source or dependency edits.

Inside Docker, `/app/venv` collects the installed application and dependencies so
the multi-stage build can copy that directory into the runtime image. It reuses
the base image's Python interpreter. This is a packaging choice, not an extra
security boundary, and the host's `venv/` is never copied into Docker. The build
and runtime stages share the same updated base for native-library compatibility.

Publishing is loopback-only by default. Set `LASTBRIDGE_HOST=0.0.0.0` in `.env` if
you need LAN access through your existing network setup. Container-internal
binding remains `0.0.0.0:5274`; `LASTBRIDGE_PORT` changes only the published port.
Account pages are available; music catalog and playback are not yet implemented.

Bootstrap or recover the container's administrator with the same hidden prompts:

```sh
docker compose exec lastbridge /app/venv/bin/python -m lastbridge.manage bootstrap-admin admin
docker compose exec lastbridge /app/venv/bin/python -m lastbridge.manage reset-password admin
```

`docker compose down` preserves named volumes. Do not add `--volumes` unless you
intend to delete all stored data.

## Container security checks

The base is digest-pinned Python 3.14.10 on Debian 13 (Trixie). Debian updates run
during the build, but Docker can cache that layer. To fetch updates again:

```sh
docker compose build --pull --no-cache
docker compose up --wait
```

Review and refresh the pinned base digest periodically too: `--pull` cannot move
a digest pin, and Debian updates do not update the base image's Python interpreter.
APT repositories are not snapshot-pinned, so OS package versions can change between
uncached builds despite the fixed base and Python dependency lock.

Trivy 0.76.0 scans on 2026-10-28, using the same vulnerability database updated at
13:01 UTC, gave these **package vulnerability finding** counts on amd64:

| Image | Critical | High |
| --- | ---: | ---: |
| Original Python 3.14.4 / Debian 12 development image | 7 | 75 |
| Previous agent's two-stage image with Debian 12 updates | 2 | 59 |
| Iteration 1 Python 3.14.10 / Debian 13 runtime image | 0 | 44 |
| Iteration 3 runtime, including cryptography | 0 | 44 |

The remaining 44 high findings correspond to eight distinct CVEs across Debian
packages, with no fixed versions listed by this database. Lower-severity findings
also remain (58 medium, 61 low, one unknown). No vulnerabilities were reported for
the installed Python application dependencies, including the iteration 3
cryptography addition. That scan reused the same database for comparison. This is a dated scan result, not
a guarantee of safety or a count of remotely exploitable application bugs. Other
scanners can group findings or assign severity differently.

For a repeatable local scan without mounting the Docker socket into the scanner:

```sh
mkdir -p /tmp/lastbridge-scan
docker image save -o /tmp/lastbridge-scan/image.tar lastbridge-lastbridge:latest
docker run --rm -v /tmp/lastbridge-scan:/scan \
  aquasec/trivy:0.76.0@sha256:2416d1f74045b57274df1a340eee6b510cacd73c66a6ccb8da092b117eb7d222 \
  image --scanners vuln --input /scan/image.tar
```

Sources: [uv multi-stage Docker packaging](https://docs.astral.sh/uv/guides/integration/docker/)
and [Trivy severity selection](https://trivy.dev/docs/v0.76/guide/scanner/vulnerability/#severity-selection).

## Checks

```sh
venv/bin/ruff check .
venv/bin/ruff format --check .
venv/bin/pytest
docker compose config --quiet
```

Tests use temporary storage and do not contact providers. They cover lifecycle,
configuration, account/session isolation, CSRF and callback replay protection,
credential encryption/recovery, protocol authentication, credential-safe logging,
recording/version matching, format
selection, worker/child deadlines, and partial-file cleanup. A synthetic audio
test uses loopback HTTP and the real media tools when installed; it explicitly
skips when FFmpeg/ffprobe are absent. Live YouTube checks are never a unit-test
requirement.

## Validation record

Iteration 1, 2026-10-28:

- Local Python 3.14.4; uv 0.13.6 installed inside `venv/`.
- Updated container runtime: Python 3.14.10 on Debian 13.7, digest-pinned base.
- Locked FastAPI 0.162.0, Uvicorn 0.54.0, HTTPX 0.28.1, and Pydantic 2.14.1.
- Nine tests pass locally and in Docker, including real loopback HTTP
  startup/shutdown and credential-bearing dummy requests at DEBUG log level.
  Container test tooling is installed only in a disposable test container;
  the runtime image excludes it.
- Ruff lint/format checks and Compose configuration validation pass.
- The Docker image builds and Compose reaches healthy status on amd64. Both
  health routes respond, and the service runs as UID 10001. Other architectures
  have not been tested.
- The current Starlette test client emits one deprecation warning about its
  HTTPX integration; tests pass. No warning is suppressed by project settings.
- In the agent's restricted sandbox, TestClient's thread/event-loop handoff
  stalled. The same suite runs normally outside that sandbox and in Docker;
  tests are not allowed to silently skip this check.

Iteration 2, 2026-10-28: anonymous YouTube search/resolution and three real samples
were tested from this Ubuntu host; details are in the [probe report](docs/youtube-probe.md).
Real client playback and audio retention/caching remain unimplemented.

Iteration 3 adds SQLite accounts, browser login/admin creation, encrypted client
and Last.fm credentials, protocol authentication, and one-time browser linking.
Cryptography 50.0.2 is locked. All 59 local tests pass, with the same single
Starlette deprecation warning; lint, formatting and Compose validation pass.
The rebuilt image passed a disposable-container ASGI smoke test for bootstrap,
browser sign-in, encrypted synthetic linking state, protocol ping, and readiness
on Python 3.14.10 as UID 10001. This was not a live Last.fm exchange.
Live Last.fm linking still requires local application keys and browser review.
