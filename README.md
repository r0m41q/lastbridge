# Lastbridge

Last.fm discovery and a private saved library through existing OpenSubsonic music
clients. Python service, with YouTube Music as the planned first music provider.

**Current scope: service foundation only.** Account linking, OpenSubsonic routes,
music providers, audio caching, and playback are not implemented yet. Only health
routes are available. No API keys or music accounts are required for this step.

## Local development

Python 3.14 is the initial supported runtime; verified versions are recorded below.
All project dependencies and tooling live in `venv/`.

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

The development image includes check tools, runs as UID 10001 with one worker, and
stores data in the `lastbridge-data` named volume. An empty read-only credentials
volume is reserved for future credential files; the scaffold does not consume it.
The build context excludes local environments, `.env`, data, and Git metadata.
Restart after configuration changes and rebuild after source or dependency edits.

Publishing is loopback-only by default. Set `LASTBRIDGE_HOST=0.0.0.0` in `.env` if
you need LAN access through your existing network setup. Container-internal
binding remains `0.0.0.0:5274`; `LASTBRIDGE_PORT` changes only the published port.
This is an unauthenticated foundation, not a usable music server yet.

`docker compose down` preserves named volumes. Do not add `--volumes` unless you
intend to delete all stored data.

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
- Locked FastAPI 0.162.0, Uvicorn 0.54.0, HTTPX 0.28.1, and Pydantic 2.14.1.
- Nine tests pass locally and in Docker, including real loopback HTTP startup/shutdown and
  credential-bearing dummy requests at DEBUG log level.
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
