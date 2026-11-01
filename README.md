# Lastbridge

A self-hosted OpenSubsonic server that combines Last.fm recommendations with
YouTube Music audio. Use an existing music client; the web UI handles accounts,
Last.fm linking, and library management.

- Search artists, albums, and songs; stream, seek, and download.
- Save favorites and optionally keep whole playlists downloaded on the server.
- Get personal Comfort, Discover, Listen Again, and Loved Tracks playlists.
- Scrobble to Last.fm, import loved tracks, start radio, and read synced lyrics.
- Separate user libraries with shared audio storage.

Still in development. Feishin browsing, playback, seeking, and downloads have been
confirmed; iOS client testing is pending. Radio, autoplay, and lyrics depend on
client support and provider availability.

## Run with Docker Compose

```sh
cp .env.example .env
```

Edit `.env` before starting:

- Set `LASTBRIDGE_PUBLIC_URL` to the exact URL you will open in your browser
  (default: `http://127.0.0.1:5274`). `localhost` and `127.0.0.1` are different origins.
- Set both `LASTBRIDGE_LASTFM_API_KEY` and `LASTBRIDGE_LASTFM_API_SECRET` from your
  [Last.fm application](https://www.last.fm/api/account/create). Use
  `<LASTBRIDGE_PUBLIC_URL>/lastfm/callback` as its callback URL.
- For LAN access, set `LASTBRIDGE_HOST=0.0.0.0`; publishing is loopback-only by default.

```sh
docker compose up -d --build
docker compose exec lastbridge /app/venv/bin/python -m lastbridge.manage bootstrap-admin admin
```

There are no default credentials. The command prompts for a browser password
(minimum 8 characters) and creates the first administrator.

Open the configured URL, sign in, and link Last.fm. Generate a **client password**
on the account page, then connect your music app using the server URL, your
Lastbridge username, and that generated password. The browser password does not
work in music clients. The administrator creates additional users in the web UI;
each user links their own Last.fm account.

To reset a browser password:

```sh
docker compose exec lastbridge /app/venv/bin/python -m lastbridge.manage reset-password admin
```

Resetting also revokes that user's browser sessions and client password.

## Configuration and updates

See [.env.example](.env.example) for all settings. Defaults include a shared 1 GB
playback cache, AAC audio, and personal mix refreshes 24 hours after the last
successful refresh. Automatic mix refresh pauses after 7 days of user inactivity.

Favorites and playlists marked **Keep downloaded** retain audio outside the cache
limit. Ordinary playlists store metadata only. Linking Last.fm automatically
imports matched loved tracks as favorites and queues their downloads; manage the
import from **Last.fm sync** in the web UI.

First playback waits for the complete track to download. Audio is served without
transcoding; Opus is optional and requires client support. Enable server scrobbling
in your client and avoid running a second scrobbler for the same playback.

After changing configuration or pulling updates:

```sh
docker compose up -d --build --force-recreate
docker compose logs -f lastbridge
```

## Data and backups

Docker stores persistent data in the `lastbridge-data` named volume, mounted at
`/app/data`. Local development uses `data/` by default.

Stop the service and back up the **whole data volume** before upgrading. Keep the
SQLite database, `credentials.key`, and saved audio together: losing the key makes
encrypted credentials unrecoverable. Database upgrades run automatically; rolling
back may require restoring the backup with the matching application version.

`docker compose down` preserves data. Adding `--volumes` deletes it.

## Development

Requires Python 3.14 and FFmpeg/ffprobe on `PATH` for audio playback.

```sh
python3.14 -m venv venv
venv/bin/python -m pip install uv==0.13.6
UV_PROJECT_ENVIRONMENT=venv venv/bin/uv sync --locked --inexact
cp .env.example .env
venv/bin/python -m lastbridge.manage bootstrap-admin admin
venv/bin/lastbridge
```

Configure Last.fm and connect clients as described above. Local development and
Docker use separate storage by default.

Code is organized by feature under [src/lastbridge/features/](src/lastbridge/features/),
with `domain`, `application`, and `adapters` layers. [bootstrap.py](src/lastbridge/bootstrap.py)
wires services; [platform/](src/lastbridge/platform/) owns storage infrastructure;
[transport/](src/lastbridge/transport/) hosts shared browser and OpenSubsonic code.
Tests live in [tests/](tests/), with reusable fixtures in [tests/support/](tests/support/).

```sh
venv/bin/ruff check .
venv/bin/ruff format --check .
venv/bin/pytest
docker compose config --quiet
```

Tests use temporary storage and mocked providers. Live provider diagnostics are
opt-in: run `venv/bin/python -m lastbridge.probe --help` for options.
