# Lastbridge

Last.fm discovery and a private saved library through existing OpenSubsonic music
clients. Python service, with YouTube Music as the planned first music provider.

**Current scope: service foundation only.** Account linking, OpenSubsonic routes,
music providers, audio caching, and playback are not implemented yet. Only health
routes are available. No API keys or music accounts are required for this step.

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
locally using `venv/`. An empty read-only credentials
volume is reserved for future credential files; the scaffold does not consume it.
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
This is an unauthenticated foundation, not a usable music server yet.

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
| Current Python 3.14.10 / Debian 13 runtime image | 0 | 44 |

The remaining 44 high findings correspond to eight distinct CVEs across Debian
packages, with no fixed versions listed by this database. Lower-severity findings
also remain (58 medium, 61 low, one unknown). No vulnerabilities were reported for
the installed Python application dependencies. This is a dated scan result, not
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
readiness, storage initialization failure, configuration precedence/validation,
and avoiding credential disclosure in foundation logs. Later iterations will add
feature-specific tests and real client checks.

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

No live music provider, Last.fm account, or real music client has been tested in
this iteration. Storage directories are prepared, but audio retention/caching
behavior belongs to later iterations.
