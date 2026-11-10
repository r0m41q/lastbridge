# Lastbridge

A self-hosted OpenSubsonic server that combines Last.fm recommendations with
YouTube Music audio. Use an existing music client; the web UI handles accounts,
Last.fm linking, and library management.

- Search artists, albums, and songs; stream, seek, and download.
- Save favorites and optionally keep whole playlists downloaded on the server.
- Get personal Comfort, Discover, Listen Again, and Loved Tracks playlists.
- Scrobble to Last.fm, import loved tracks, start radio, and read synced lyrics.
- Separate user libraries with shared audio storage.
- Import public YouTube playlists, with shared charts refreshed daily.

Still in development. Feishin browsing, playback, seeking, and downloads have been
confirmed; iOS client testing is pending. Radio, autoplay, and lyrics depend on
client support and provider availability.

Search puts YouTube Music's recommended artists, albums, and tracks from its
search predictions first, followed by regular results. These appear in your
client's existing search results; no separate autocomplete UI is required.
If predictions are unavailable, regular search remains available.

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
- Set `LASTBRIDGE_DATA_DIR_HOST` to the NVMe directory for the database and bounded
  cache, and `LASTBRIDGE_SAVED_AUDIO_DIR_HOST` to the HDD directory that Jellyfin
  scans. These are host paths only; the container paths stay fixed.

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
Administrators can also reset passwords from **Administration**, which shows
users, saved storage, cache and disk usage, and job progress with retry controls.

## Configuration and updates

See [.env.example](.env.example) for deployment settings. The audio size
environment variables are absolute ceilings; the Administrator chooses equal or
lower runtime values from **Administration**. Streaming provider, downloading
provider, format, timeout, cache, file-size, free-space, Jumo URL, region, and
quality settings are stored in the Lastbridge database. YouTube Music is the
default for both roles; Jumo-DL and the public [Rhythmax JioSaavn](https://saavn.odskyler.com/) provider are
available alternatives, and YouTube can be enabled as a fallback. Rhythmax
requests 320 kbps AAC source audio; the server preserves AAC or converts to
Opus at a 320 kbps target. Defaults include a shared 1 GB playback cache, AAC
audio, and personal mix refreshes 24 hours
after the last successful refresh.

Rhythmax preserves a selected track's explicit/clean flag when available.
Equivalent recordings on other albums are eligible: the selected album wins,
then the closest duration (within five seconds), then provider result order.
Live/remix/version qualifiers in track titles must still match. Older catalog
entries without an explicit flag prefer explicit, then unknown, then clean
recordings. Known version mismatches use the configured YouTube fallback.

Lyrics are served through synced `getLyricsBySongId` and legacy `getLyrics`.
The legacy endpoint resolves artist and title against the existing catalog and
returns plain text from the same LRCLIB lookup and cache. Missing names or an
unknown catalog song return empty lyrics. Lyrics are not embedded in saved files.

Favorites and playlists marked **Keep downloaded** retain audio outside the cache
limit. Ordinary playlists store metadata only. Linking Last.fm automatically
imports matched loved tracks as favorites and queues their downloads; manage the
import from **Last.fm sync** in the web UI.

Import a public YouTube playlist from **Playlists** in the browser. To add a
shared chart, choose **Everyone** in the visibility field, preview it, and
confirm **Add shared chart**. Preview shows unavailable or unmatched tracks
before saving. Imports are read-only, metadata-only snapshots; copy one to edit
or keep it downloaded. Shared charts refresh once every 24 hours after their
last successful refresh, including while users are inactive. Private imports
refresh on request.
Up to 1,000 entries and 50 exact-audio fallback searches per import are supported.
Private YouTube accounts and Spotify/Apple Music integrations are not supported.

First playback waits for the complete track to download. Audio is served without
transcoding; Opus is optional and requires client support. Enable server scrobbling
in your client and avoid running a second scrobbler for the same playback.

After changing configuration or pulling updates:

```sh
docker compose up -d --build --force-recreate
docker compose logs -f lastbridge
```

## Data and backups

Docker stores the database, credentials, and bounded playback cache in
`/app/data`. For a server deployment, bind-mount that directory to NVMe and
bind-mount `/app/data/saved/audio` separately to the HDD music library. Saved
audio is the permanent copy and can be scanned by Jellyfin; the cache remains
temporary and bounded.

Saved recordings preserve the selected audio policy and container. Lastbridge
does not transcode audio for Jellyfin: AAC/M4A and Opus/WebM remain separate
supported policies, and clients must support the selected format.

The container runs as UID 10001. The host paths must be writable by UID 10001
and readable/traversable by the Jellyfin process. For a dedicated Lastbridge
subtree, for example:

```sh
sudo mkdir -p /mnt/nvme/docker-manual-data/lastbridge \
  /mnt/hdd/media/music/Lastbridge
sudo chown -R 10001:10001 /mnt/nvme/docker-manual-data/lastbridge \
  /mnt/hdd/media/music/Lastbridge
sudo chmod -R u+rwX,go+rX /mnt/nvme/docker-manual-data/lastbridge \
  /mnt/hdd/media/music/Lastbridge
```

If Jellyfin runs with a restricted UID or group, use a shared group or ACL
instead of broadening permissions on the parent media tree. Do not grant
Lastbridge ownership of unrelated media directories.

Jellyfin scanning is independent. Optionally set
`LASTBRIDGE_JELLYFIN_SCAN_URL` to a reachable `POST` refresh endpoint such as
`http://jellyfin:8096/Library/Refresh` and provide
`LASTBRIDGE_JELLYFIN_API_KEY`. Lastbridge requests a scan only after a saved
file reaches `ready`; a failed optional request does not fail the download.
For bulk imports, scheduled or manual Jellyfin scans are preferable.

Stop the service and back up both `LASTBRIDGE_DATA_DIR_HOST` (NVMe) and
`LASTBRIDGE_SAVED_AUDIO_DIR_HOST` (HDD) before upgrading. Keep the SQLite database,
`credentials.key`, and saved audio together: losing the key makes encrypted
credentials unrecoverable. Database upgrades run automatically; rolling back
may require restoring the backup with the matching application version.

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
