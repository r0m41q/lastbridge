# Lastbridge

Last.fm discovery and a private saved library through existing OpenSubsonic music
clients. Python service, with YouTube Music as the first music provider.

**Current scope: accounts, Last.fm linking, authenticated catalog search and
browsing, artwork, health routes, and a developer-only YouTube probe.** Audio
streaming, caching, favorites, and recommendations arrive in later iterations.
Actual Amperfy/Feishin onboarding still needs manual verification.

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

Configure one [Last.fm application](https://www.last.fm/api/account/create) for
the server. Use `<public URL>/lastfm/callback` as its callback URL. Put its key
and shared secret in your ignored `.env` file:

```dotenv
LASTBRIDGE_PUBLIC_URL=http://127.0.0.1:5274
LASTBRIDGE_LASTFM_API_KEY=YOUR_32_CHARACTER_API_KEY
LASTBRIDGE_LASTFM_API_SECRET=YOUR_32_CHARACTER_SHARED_SECRET
```

Docker Compose passes these variables to the container; local development reads
the same `.env`. No credential JSON file or mount is needed. The old
`LASTBRIDGE_LASTFM_CREDENTIALS_FILE` setting is removed. Keep real values out of
source control and chat. Both empty means unconfigured: the service still starts
but linking is unavailable. Setting only one or using malformed credentials fails
configuration validation without echoing their values. Settings representations
and JSON serialization mask these fields.

`LASTBRIDGE_PUBLIC_URL` is the exact browser origin, without a path prefix, query,
or fragment. For the default setup, use `http://127.0.0.1:5274/login` consistently;
`localhost` is a different origin. Use your external HTTPS origin when deploying.
After editing `.env`, recreate the container to load its environment:

```sh
docker compose up -d --force-recreate
```

After source changes, include `--build` as well. Reload the browser account page
and choose **Link Last.fm**. Existing local accounts and Last.fm links remain in
the persistent data volume.

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
authenticate protocol calls. Public extension discovery advertises API key
authentication and form POST only; other extensions are not implemented yet.

`/rest/ping` and `/rest/ping.view` support GET and URL-encoded form POST, XML
(default) and JSON (`f=json`). Supply `v` and `c`. Errors use protocol responses:
40/44 for bad credentials, 43 for conflicting mechanisms, 50 for missing/revoked
Last.fm linking, and 0 for unimplemented endpoints. Normal music access requires
a linked account; administration remains accessible before linking.

SQLite schema version 2 is created transactionally on first startup; version 1
is upgraded automatically without changing accounts or credentials. Newer schema
versions are refused. Browser passwords use salted scrypt; browser session tokens
and callback state are stored as SHA-256 digests. The generated client credential
and Last.fm session key use Fernet authenticated encryption. The database and
`data/credentials.key` use owner-only file permissions. Back up **both** together
with the application stopped; loss of the key makes encrypted credentials
unrecoverable. Startup refuses to replace a missing key or accept a mismatched
one. Encryption protects a database-only copy; anyone who can read the complete
data volume can also read its key. Browser pages return no-store and
same-origin referrer headers, escaped text, CSRF-protected forms, and HttpOnly
session cookies; cookies
use Secure when the configured public origin is HTTPS.

The implementation follows [Last.fm web authorization](https://www.last.fm/api/webauth)
and the [OpenSubsonic authentication contract](https://opensubsonic.netlify.app/docs/api-reference/).
Tests use a mocked provider exchange; **real Last.fm authorization has not been
tested with live application keys**. For review, bootstrap an administrator,
create a second account, link each to its own Last.fm account, restart the service,
and verify each link remains associated with the correct local account. Catalog
review instructions follow; playback belongs to the next iteration.

## Catalog browsing and client review

Select a generic **Subsonic/OpenSubsonic** server in the client, enter the
Lastbridge public URL, your local username, and the generated client password.
Link Last.fm through the browser first. Browser passwords do not work in clients.
Client connection, search, and browsing are the review targets for this iteration;
playback buttons may be disabled because streaming/download roles are still false.

Implemented read endpoints support XML/JSON, GET/form POST, and `.view` aliases:

- Public `getOpenSubsonicExtensions`: `apiKeyAuthentication` and `formPost` v1.
- Authenticated `ping`, `getLicense`, and the caller's own `getUser` profile.
  Administrative account management stays in the browser.
- `search3` (tag results), `search2` (folder-style album results), `getSong`,
  `getAlbum`, `getArtist`, `getMusicDirectory`, and `getCoverArt`.
- Saved-library enumeration: `getMusicFolders`, `getArtists`, `getIndexes`,
  `getAlbumList`/`getAlbumList2`, `getGenres`, `getStarred`/`getStarred2`,
  `getPlaylists`, and `getScanStatus`. These return an empty library until saving
  and playlist features arrive. `getPlaylist` returns not-found.

An empty/whitespace search query (also the literal `""` used by some clients)
returns the user's saved library. A nonempty query searches anonymous YouTube
Music metadata, even with an empty saved library. Searching does not save or
download tracks. Song results include official catalog audio; ordinary music
videos and unavailable tracks are excluded. This is interactive search, not
automatic matching of a requested recording to one result.

Each result type has independent count/offset pagination. Counts are 0–100;
remote discovery is capped at the provider's first 100 candidates per type.
Offsets beyond that window return an empty page. Short-lived per-user search
snapshots preserve ordering for 60 seconds; at most 32 snapshots are held in RAM.
Queries are not persisted. This cap is not a promise of exhaustive catalog search.

Artist pages currently show the provider's initial albums/singles sections, not
an exhaustive discography. Album browsing hydrates its available tracks (at most
1,000), preserving their positions when an unavailable track is omitted. Search
album counts/durations are initially zero where unknown; opening the album fills
them. The required album `created` field is its first local metadata insertion,
not its release date. Track bitrate/codec/size fields remain absent until actual
audio resolution; metadata does not establish sound quality.

Public artist/album/song metadata is shared internally. Stable IDs derive from
provider identity, survive restart, and do not contain expiring stream URLs.
Any linked user can retrieve an already-known public catalog ID; this does not
grant membership in anybody else's saved library. Missing/wrong-type IDs return
70. Account/profile access across users returns 50, including for browser admins.

Provider requests run in disposable subprocesses with a 30-second deadline, two
concurrent workers, a one-second admission wait, and an 8 MiB response limit.
Failures return a fixed protocol error without dependency output or upstream
URLs. Previously hydrated metadata remains browsable during provider outages;
current hydrated detail pages are reused without automatic refresh. Search
snapshots expire, but persistent catalog metadata has no eviction policy yet.

Artwork is fetched only after authentication, from allowlisted HTTPS Google image
hosts, without redirects, client credentials, or returned upstream URLs. Responses
are limited to 2 MiB and JPEG/PNG/WebP media types. The `size` parameter is accepted
(up to 4,096) but advisory: the existing provider thumbnail is returned unchanged.
Images are not persistently cached in this iteration.

For manual review in **Amperfy and Feishin**:

1. Connect using the generated credential; confirm an empty saved library syncs.
2. Search Daft Punk — Instant Crush, Joji — PIXELATED KISSES, and OTOY — Судоми.
3. Open a song's album and artist; verify titles, relationships and artwork.
4. Repeat with a second linked user; neither search should populate saved music.
5. Restart and reopen previously visited items; report client/version and any
   failed operation. Playback and favorite changes are outside this review.

The mapping follows the [OpenSubsonic search contract](https://opensubsonic.netlify.app/docs/endpoints/search3/)
and [public extension discovery requirement](https://opensubsonic.netlify.app/docs/endpoints/getopensubsonicextensions/),
using [ytmusicapi browsing](https://ytmusicapi.readthedocs.io/en/stable/reference/browsing.html).
Automated protocol checks and real provider checks do not substitute for actual
client/device verification.

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
image. The service now includes ytmusicapi for metadata; audio extraction remains
optional and outside the runtime image.

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
| `LASTBRIDGE_LASTFM_API_KEY` | empty | Last.fm application API key; set together with the secret |
| `LASTBRIDGE_LASTFM_API_SECRET` | empty | Last.fm application shared secret |

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
locally using `venv/`. Last.fm application credentials are supplied through
`LASTBRIDGE_LASTFM_API_KEY` and `LASTBRIDGE_LASTFM_API_SECRET` from the ignored
`.env`; no credentials volume is required. Existing unused credential files or
volumes are not deleted automatically.
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
Account pages and catalog browsing are available; audio playback is not yet
implemented.

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
| Iteration 4 runtime, including ytmusicapi | 0 | 44 |

The remaining 44 high findings correspond to eight distinct CVEs across Debian
packages, with no fixed versions listed by this database. Lower-severity findings
also remain (58 medium, 61 low, one unknown). No vulnerabilities were reported for
the installed Python application dependencies, including cryptography and the
iteration 4 metadata dependencies. These scans reused the same database for
comparison. This is a dated scan result, not
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
catalog relationships and restart, migration from v1, XML/JSON mapping, search
pagination/library separation, artwork restrictions and metadata worker limits,
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

Iteration 4, 2026-10-28: catalog persistence/migration, public extension discovery,
private profile access, remote search, song/album/artist and folder browsing,
and authenticated artwork are implemented. All 97 tests pass in 20.73 seconds,
with the same Starlette warning; lint, formatting, diff checks and Compose
configuration pass. The runtime image builds and its disposable fixture-based
ASGI catalog smoke test passes as UID 10001 on Python 3.14.10/ytmusicapi 1.12.3.
The updated image scan reports zero Python dependency findings; OS counts remain
0 critical, 44 high, 58 medium, 61 low and one unknown using the same database.

Live anonymous metadata checks through the new worker succeeded for all three
agreed queries. First song results resolved to Random Access Memories (13 tracks),
Piss In The Wind (21 tracks), and Судоми (one track), with artist release pages
and JPEG artwork. These checks used a disposable database and fetched no audio.
Actual Amperfy/Feishin connection, synchronization, search and browsing remain
pending manual review. No client playback or live Last.fm linking is claimed.

Browser login correction: use `Referrer-Policy: same-origin` so browsers retain
the Origin header on form POSTs. The previous `no-referrer` policy could send
`Origin: null`, causing valid logins to fail CSRF validation. Origin/token checks
remain enabled. After pulling source changes, rebuild/recreate the container and
reload the login page; existing administrator accounts remain in the data volume.
The correction passes all 98 tests. An isolated real-browser form check also
confirmed the old policy sends `Origin: null` and the new policy preserves the
form's origin; the user's live administrator login still needs a retry.

Last.fm configuration now uses environment variables directly. The updated
103-test suite and two additional CLI credential-redaction tests pass; lint,
formatting and Compose configuration validate. No live keys were used for these
checks. Rebuild the image after this source change, then recreate the container
with the two variables set in the ignored `.env`.

Last.fm callback tokens and session keys are treated as bounded, printable ASCII
values rather than hex strings: current web authorization can return URL-safe
non-hex tokens. Browser-session binding, ten-minute local expiry, atomic claims
and replay protection remain enforced. Start a fresh link from the account page
after rebuilding; do not reuse or refresh an old callback URL.

Feishin 1.17.0 manual review confirmed login, remote search and search artwork;
its album page initially stayed loading. Feishin's song parser unconditionally
reads `contentType.startsWith(...)`. Unresolved tracks now return the generic
`application/octet-stream` MIME type, including already-cached album/song and
folder responses. This reports an unknown format without inventing an audio
codec, bitrate, suffix or file size. Cached metadata does not need deleting or
refetching. The album page still needs a user retry after rebuilding. Empty home
screens and unavailable playback remain expected until later iterations.
