# Lastbridge

Last.fm discovery and a private saved library through existing OpenSubsonic music
clients. Python service, with YouTube Music as the first music provider.

**Current scope: accounts, Last.fm linking, catalog browsing, audio playback,
seeking/downloads, private favorites, shared permanent audio retention,
Last.fm scrobbling, resumable loved-track imports, and personal mix playlists.**
Radio continuation arrives in a later iteration. The user confirmed
Feishin browsing, playback, seeking, and downloading; Amperfy testing is deferred.
Favorites, Last.fm sync, and personal mixes have manual review steps below.

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
relink state; scrobble delivery marks it on provider error 9. No background
revocation polling is implemented, and transient
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
40/44 for bad credentials, 43 for conflicting mechanisms, 50 for missing initial
Last.fm linking, and 0 for unimplemented endpoints. Music clients require an
initial link; revoked Last.fm authorization pauses delivery while preserving
local playback. Administration remains accessible before linking.

SQLite schema version 5 is created transactionally on first startup; versions 1,
2, 3 and 4 are upgraded automatically without changing accounts, credentials or catalog
identities. Back up the data volume before upgrading. Newer schema
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

To preserve downloaded favorites as well as membership, include `data/saved/`
with its audio files and manifests in that stopped-volume backup. Restoring only
SQLite and the credential key preserves save intent but requires reacquisition of
missing audio from the provider. Older application versions cannot open schema 5;
restore the matching pre-upgrade backup for a rollback.

The implementation follows [Last.fm web authorization](https://www.last.fm/api/webauth)
and the [OpenSubsonic authentication contract](https://opensubsonic.netlify.app/docs/api-reference/).
Tests use a mocked provider exchange; **real Last.fm authorization has not been
tested with live application keys**. For review, bootstrap an administrator,
create a second account, link each to its own Last.fm account, restart the service,
and verify each link remains associated with the correct local account. Catalog
and playback review instructions follow. The user has confirmed account linking
and Feishin browsing; the agent has not accessed live Last.fm credentials.

## Playback, seeking, and audio cache

Rebuild the image and restart the client to enable playback:

```sh
docker compose up -d --build --force-recreate
```

Authenticated `stream` and `download` (including `.view` aliases) return original
source audio. GET and form POST are supported; GET/HEAD support HTTP byte ranges,
including suffix/multiple ranges, `If-Range`, and 416 for unsatisfiable ranges.
Downloads use attachment filenames. No provider URL, cookie, or header reaches
the client. Requests do not create favorites or count as listened/scrobbled plays.
The endpoints follow the [stream](https://opensubsonic.netlify.app/docs/endpoints/stream/)
and [download](https://opensubsonic.netlify.app/docs/endpoints/download/) contracts.

First playback waits for the **complete track** to download and pass container,
duration, and full decoding checks. Cached playback and seeking use the completed
local file. Concurrent requests for the same recording/rendition share one job;
a cancelled waiter does not cancel another listener's acquisition. At most two
recordings download at once; excess requests get a retryable protocol error.
Acquisition has a 180-second deadline and kills the worker process group on
shutdown/timeout. Each new attempt resolves fresh URLs; an expired URL is never
persisted. A failed transfer can be retried from scratch, without publishing its
partial bytes. This implementation does not progressively stream incoming audio.

`LASTBRIDGE_AUDIO_POLICY=aac` selects the best available AAC/M4A source, with no
transcoding or upscaling. Explicit `opus` selects Opus/WebM for comparison; it
requires client support. The policy applies equally to all users and networks.
If that codec is unavailable, playback fails explicitly. Client `format` and
`maxBitRate` are preferences and do not change this source policy. No transcoding
or transcode-offset extension is advertised; seek with HTTP Range instead of
`timeOffset`. Leave client transcoding disabled during review.

The runtime includes locked yt-dlp and minimal FFmpeg/ffprobe binaries built
from Debian-authenticated FFmpeg 7.1.5 source. Anonymous `visionos`
extraction is the currently verified path. It uses no YouTube login, browser
cookies, JavaScript runtime, global downloader configuration, or remote scripts.
Local runs need `ffmpeg` and `ffprobe` on PATH; Docker provides them. Provider
changes or a different deployment IP may still prevent acquisition.

Temporary audio lives in `data/cache/audio/`; retained recordings live in
`data/saved/audio/`. Completed files
are published atomically with small manifests. Restart removes only this cache's
private staging files and uncommitted audio, and reuses valid completed files.
The cache's exclusive lock prevents a second process using the same audio tree.
Run one application worker. Catalog IDs and relationships survive audio eviction.

Configuration (bytes; also forwarded by Compose):

| Variable | Default | Meaning |
| --- | ---: | --- |
| `LASTBRIDGE_AUDIO_CACHE_BYTES` | 1,000,000,000 | Shared audio budget, about 1 GB |
| `LASTBRIDGE_AUDIO_FILE_MAX_BYTES` | 67,108,864 | Maximum recording, clamped to half the cache budget |
| `LASTBRIDGE_AUDIO_FREE_RESERVE_BYTES` | 268,435,456 | Free disk reserve before/while acquiring |

The cache reserves two file allowances per acquisition (source plus possible
MP4 container remux), then evicts completed
least-recently-used entries as needed. Active responses and pending listeners
protect their files. Staging plus completed temporary audio stays within the budget;
small manifests/filesystem overhead are additional. A process-level file limit
also bounds unknown-length downloads. If all candidates are active, or free disk
is insufficient, new acquisition fails safely. Existing cached playback remains
available. Saved audio is outside that temporary budget; its file-size and free
disk limits still apply during acquisition. Phone offline downloads are independent.

## Favorites and permanent downloads

Heart a song in the client to add a private favorite immediately and queue a
persistent save job. The matching album and artist become browsable in your
saved library. Hearting an album first resolves its available tracks, then saves
that track snapshot as one operation. Missing/provider-unavailable albums return
an error without half-saving a batch. Album tracks enter the library without
individually setting their hearts. An artist heart is a metadata bookmark and
does not download an entire discography. A song heart inside a saved album is an
independent retention reason. Local hearts do not change Last.fm loves.

`star`/`unstar` support song/folder `id`, `albumId`, and `artistId`, including
repeated parameters in GET/form POST. Batches validate before membership changes;
repeating a heart preserves its original timestamp. Requests have a 128-parameter
limit (including authentication) and a 16 KiB form limit. Duplicate authentication
parameters remain invalid. `getStarred`/`getStarred2`, search, and detail responses
show only the caller's heart timestamps; shared catalog documents contain none.
These follow the [star](https://opensubsonic.netlify.app/docs/endpoints/star/),
[unstar](https://opensubsonic.netlify.app/docs/endpoints/unstar/), and
[getStarred2](https://opensubsonic.netlify.app/docs/endpoints/getstarred2/) contracts.

Open **Saved library and download jobs** in your browser account (or `/library`)
to see your pending, running, saved, and failed recordings. Reload to update the
table. One background save runs at a time, sharing in-flight acquisition and
completed audio with playback. Initial failure retries after 30 seconds, then
120 seconds; the third failure waits for your retry action. A failure keeps the
heart and library membership. The CSRF-protected retry button affects only jobs
for your saved recordings. Interrupted running jobs resume after restart;
completed files are reused. If SQLite itself cannot write, the worker waits for
storage recovery; a new heart needs a successful database write to be accepted.

Audio is shared by recording identity and configured rendition. Two users saving
one recording consume one physical audio file. Favorite pins, including album
track pins, prevent eviction. Removing a heart releases only that favorite's
pins; another heart, an album, another user, or an active response can still keep
the recording. After the last pin and active transfer are gone, audio returns to
the LRU cache and becomes evictable; metadata IDs remain valid. Promotion/demotion
uses hard links on the same data filesystem, preserving any active response's
old path until completion. Keep cache and saved audio on the same filesystem;
the existing Compose volume already does this. Cleanup blocked by a filesystem
error is retried on reconciliation/restart without deleting retained music.

There is no total saved-library quota. Low disk stops new acquisition while
completed local playback remains usable. Size/manifest corruption in committed
saved audio fails startup for repair from backup, preserving the damaged files;
a missing saved file is queued for reacquisition. Changing the global audio
policy queues the new rendition for existing favorites and releases old-policy
copies to the cache; compare AAC/Opus before accumulating a large saved library.
Downloaded playlists, per-user size accounting, and fuller admin controls remain
later iterations.

For this iteration's review, rebuild with `docker compose up -d --build`, then:

1. In Feishin, heart a song and refresh Favorites/your library. Check its job on
   `/library`; once saved, restart Lastbridge and play/seek/download it again.
2. Heart an album and check that its available tracks enter your library. Heart
   one song independently, then unheart the album: that song should remain.
3. Save the same song from two accounts. Unheart it in one account and verify the
   other still sees its own heart and can play it. Neither sees the other's
   favorites or download table.

Amperfy checks can wait until an iOS device is available. Recommendations remain
future work. Last.fm sync review follows.

## Last.fm scrobbling and loved-track import

Linking Last.fm automatically creates a private loved-track import. Existing
linked accounts also receive initial import intent on startup after upgrading.
Open **Last.fm import and scrobble status** on your browser account (`/lastfm/sync`)
for progress, pause/resume, refresh, unmatched tracks, and delivery status. Reload
to update; `/library` shows the resulting audio download jobs.

The importer fetches 50 loved tracks per page using
[user.getLovedTracks](https://www.last.fm/api/show/user.getLovedTracks). It stores
each page and its next-page cursor together, then gradually resolves recordings,
with at least two seconds between import operations per user. Pause state and
progress survive restart. Pausing prevents new import work; an already committed
favorite/download can finish. Refresh scans pages again without removing any
local favorites or automatically retrying unmatched tracks. Use **Retry unmatched
recordings** for another matching attempt. Last.fm's changing page order is not a
snapshot: if loves change during import, refresh afterward to catch shifted rows.

Matching requires an exact normalized title and artist credit in official YouTube
Music album audio. Version qualifiers stay significant. Multiple eligible video
IDs are ambiguous and remain unmatched; live versions and covers are not selected
to fill gaps. Last.fm loved pages supply no album or duration, so this deliberately
favors leaving a track unmatched over silently choosing an edition. A match becomes
a private song favorite and queues the same permanent save job as a client heart.
Transient lookup/storage failures get three attempts before becoming unmatched.
Last.fm read outages retain the page cursor and retry after 60 seconds; permanent
API errors pause the import and appear on the status page.

Unhearting a song records a private local removal, including the imported recording
identity. Refresh, restart and retry cannot silently restore it. A deliberate client
heart clears that song's removal. Removing an album releases its album snapshot;
independent song favorites, including imported loves, still retain their songs.
Local hearts/unlikes never call Last.fm `track.love` or `track.unlove`. Linking a
different Last.fm account starts its import while preserving the local library.

The [OpenSubsonic scrobble endpoint](https://opensubsonic.netlify.app/docs/endpoints/scrobble/)
accepts GET/form POST, repeated song IDs and corresponding optional millisecond
start times. Entire batches validate before recording anything; HEAD cannot write.
The existing 128-parameter/16 KiB limits also apply. `submission=false` only records
a local start marker; it sends no Last.fm now-playing request and adds no play.
`submission=true` (the default) records a private play and queues signed HTTPS
[track.scrobble](https://www.last.fm/api/show/track.scrobble) delivery. Streaming,
seeking and downloading alone never create plays or favorites.

Clients must report submissions after half the track or four minutes, whichever
comes first, as described in [Last.fm's scrobbling rules](https://www.last.fm/api/scrobbling).
The server trusts that client signal because this endpoint carries no measured
listening duration; tracks of 30 seconds or less, and unknown durations, are kept
as local ignored reports. Own song/search/detail responses expose own play counts
and last-play timestamps without persisting them in shared metadata. Do not enable
another Last.fm scrobbler for the same playback: independently submitted reports
cannot be deduplicated by this server.

Reports deduplicate by local user, linked Last.fm account, song ID and UTC start
second. Feishin omits `time`; the server uses its previous start notification for
the same client/song. Without one, receipt time is used. Untimed submissions also
have a 30-second retry window, which can collapse rapid repeat reports. The legacy
protocol cannot distinguish concurrent plays of the same song by the same account
at the same start second. Explicit timestamps are the strongest retry identity.

The durable queue sends older pending reports first within an account. Other
users advance independently. Connection failures and Last.fm errors 11, 16 and 29
retry with exponential backoff from 30 seconds to one hour. Rejected/ignored
reports retain their status; provider messages and credentials are never shown.
Error 9 requests relinking, while existing clients retain local playback access.
Queued reports are bound to their original Last.fm username; relinking another
account never redirects old plays to it. Relink the original account to resume
blocked reports. API configuration/authorization can be checked through `getUser`
(`scrobblingEnabled`); never-linked users still cannot connect music clients.

Read/write timeouts, malformed delivery responses and interrupted in-flight jobs
become **uncertain**. They may already exist on Last.fm, so automatic retries stop.
Check your Last.fm history before using the separate **Retry uncertain reports**
button, which explicitly warns about duplicates. Exactly-once delivery to Last.fm
is not guaranteed. Other failed/blocked reports have a separate retry action for
the currently linked account. Forms require your browser session and CSRF token;
the page shows up to 200 unmatched/removed records and the latest 100 reports.
The database retains all deduplication records and import/removal intent; no
history pruning or total metadata quota is implemented yet.

For review, back up the stopped data volume before upgrading to schema 5, then
rebuild with `docker compose up -d --build`. Upgrading automatically starts imports
for configured linked accounts and may grow permanent storage. Check the sync
page and pause imports there if needed.

1. Open `/lastfm/sync`. Verify your loved tracks start importing, matches appear
   in Feishin's saved library and `/library`, and uncertain/unmatched entries are
   visible. Pause, restart, and resume to check progress persists.
2. Unheart an imported song, refresh loves, and verify it stays removed. Heart
   it deliberately to save it again. Check another user has separate progress.
3. Reconnect Feishin, enable server scrobbling in its settings, and listen past
   the qualifying threshold. Check the report becomes `sent` and appears once
   in your own Last.fm history. A start/skip or download alone should not add one.
4. If Last.fm is unavailable, verify cached playback continues and reports wait
   for retry. Review any `uncertain` reports against Last.fm before retrying them.

Automated checks use mocked Last.fm responses and disposable storage. No real
Last.fm scrobbles or loved imports have been sent by the agent; client/device
acceptance remains manual. No new credentials or configuration variables are needed.

## Personal mix playlists

After linking Last.fm, open **Playlists** in your music app. Lastbridge exposes
four private, server-managed, read-only lists through
[getPlaylists](https://opensubsonic.netlify.app/docs/endpoints/getplaylists/) and
[getPlaylist](https://opensubsonic.netlify.app/docs/endpoints/getplaylist/).
Playlist IDs stay stable across refreshes and restarts. Each user's IDs and
contents are separate; even administrators cannot read another user's mixes
through these endpoints. Playlist create/edit/delete remains deferred.

| Playlist | Selection |
| --- | --- |
| Comfort | Shuffled saved songs and six-month Last.fm top tracks; familiar listening. |
| Discover | Similar-track candidates from up to three daily rotating familiar seeds, excluding known tracks and local removals. |
| Listen Again | Up to 50 recent local completed-play reports, followed by up to 50 Last.fm recent tracks; duplicates removed. |
| Loved Tracks | All confidently matched imported Last.fm loves that remain individually hearted locally. |

The first three lists contain **up to 30 resolved tracks each**. Loved Tracks
follows the existing import and local hearts immediately, without a 30-track
limit. A local heart without an imported Last.fm love belongs to Comfort and the
saved library, rather than Loved Tracks. Local removals are filtered from all
mixes immediately. Hearting again clears the removal as usual.

Taste data comes from the first 50
[six-month top tracks](https://www.last.fm/api/show/user.getTopTracks),
[top artists](https://www.last.fm/api/show/user.getTopArtists), and
[recent tracks](https://www.last.fm/api/show/user.getRecentTracks), plus the user's
saved songs, imported loves and local playback reports. Now-playing rows do not
count as listening. Listen Again preserves newest-first order within each source,
with local reports first; it does not claim a single merged historical timeline.
The profile and candidate decisions stay in the user's own database row. Public
song/album metadata and played audio retain their existing shared storage model.

Discover uses [track.getSimilar](https://www.last.fm/api/show/track.getSimilar),
interleaving results from its seeds. If a profile has artists but no seed tracks,
their top tracks supply candidates. A completely empty profile gets
[chart suggestions](https://www.last.fm/api/show/chart.getTopTracks) in Discover;
Comfort and Listen Again wait for actual favorites/listening. Discover excludes
all locally known saved, loved and reported recordings, plus the sampled remote
top/recent tracks. Newly hearted or locally reported tracks disappear from its
current view immediately. This is discovery relative to that sample, not a
guarantee that a track has never appeared anywhere in the user's Last.fm history.

Candidate matching reuses the strict import matcher: exact normalized artist and
title among official available album recordings, preserving live/remix qualifiers.
Ambiguous, unavailable and duplicate recordings are omitted. No unrelated cover
or different version is substituted. Cached metadata is not proof of ongoing
upstream availability; a track can become unavailable before actual playback.

A single background worker refreshes daily, independently of the import and
scrobble workers. Linking and client playlist requests never wait for generation.
Each refresh allows at most 40 new recording lookups and a 180-second total
deadline, with bounded provider responses and a 250 ms gap after successful reads
and lookups. Previously resolved entries can be reused; short lists are normal
when matching is ambiguous or the budget is exhausted. Refreshes store metadata
only and do **not** queue audio or permanently pin recommendations. Playback
uses the usual temporary cache; hearts still queue permanent copies.

The three generated snapshots and taste profile publish together in one
transaction. Last.fm/provider outages, deadlines, or an entirely unresolvable
replacement for a previously populated list retain the previous snapshots and
schedule a retry in five minutes. Interrupted refreshes keep the old snapshots
and become eligible again after one minute on restart. Changing the linked
Last.fm identity clears the old profile/snapshots, and in-flight old results
cannot publish; saved local favorites remain as before. Revoked authorization
pauses refreshes while existing playlists and cached playback remain available.

Open `/mixes` from the account page to see counts, last successful refresh and
errors, or request a refresh. Manual requests require browser session/CSRF and
have a five-minute cooldown. Refreshing does not rewrite a queue already loaded
by a client. This iteration does not provide automatic endless radio or custom
client home-screen sections.

Review after backing up the stopped whole data volume and rebuilding with
`docker compose up -d --build`:

1. Open `/mixes`, allow up to three minutes for initial generation, then reload.
   Open Playlists in Feishin and check all four lists and their track counts.
2. Play and seek a Discover entry, then heart it. It should appear in the saved
   library/download jobs and disappear from Discover after reloading its list.
3. Refresh mixes in the browser. Confirm playlist IDs stay stable and a second
   account has separate contents. Local unlikes must stay removed.
4. Restart and verify snapshots remain. An empty Last.fm profile should have
   chart suggestions in Discover when confident matches are available.

Automated tests use synthetic Last.fm data and mocked audio acquisition. Real
recommendation coverage, latency and Feishin presentation remain user acceptance
checks; Amperfy/device testing is still explicitly deferred. No live credentials
or accounts were used by the agent for this iteration.

Host acquisition on 2026-10-28 verified all six complete renditions, with no
10-second clipping. Initial download times were 1.9–3.3 seconds, before adding
full decode validation; these are observations, not deployment guarantees.

| Recording | Duration | AAC/M4A | Opus/WebM |
| --- | ---: | ---: | ---: |
| Daft Punk — Instant Crush | 337.6 s | 5.46 MB, 129 kbps | 6.13 MB, 145 kbps |
| Joji — PIXELATED KISSES | 110.4 s | 1.79 MB, 130 kbps | 1.78 MB, 129 kbps |
| OTOY — Судоми | 146.1 s | 2.36 MB, 129 kbps | 2.59 MB, 142 kbps |

AAC measured 44.1 kHz stereo; Opus 48 kHz stereo. These are file-average bitrates,
not proof that one codec sounds better. Full host comparison files are ignored
under `data/probe/playback/<video-id>/<aac|opus>/audio.<m4a|webm>`.

The final minimal-tool container also passed all six real-provider streams,
byte-range seeks, and byte-identical offline download responses using synthetic
linked accounts, as UID 10001. First delivery took 2.59–4.43 seconds including
remuxing and full decoding; cached seek plus download took 12–26 ms. This used an
in-process ASGI client inside Docker, not Feishin/iOS or a network proxy.

Manual review before accepting playback:

1. Rebuild, reconnect Feishin, and play each agreed song from search or its album.
   Check the full song plays, then seek forward/backward and switch tracks.
2. Repeat with Amperfy on iOS; test its offline download, then play with networking
   disabled. Record client versions, delay, and any errors.
3. Start two clients on the same song, stop one, and confirm the other continues.
   Restart Lastbridge and replay/seek a cached song.
4. Listen to the local AAC/Opus comparison files. For client comparison, set
   `LASTBRIDGE_AUDIO_POLICY=opus` locally and recreate the container. Return to
   AAC if WebM support is insufficient. The default remains provisional.
5. Test consecutive tracks on a continuous album and report whether a gap is
   audible. Gapless playback depends on client behavior too; it is unverified.
6. Repeat acquisition from the actual deployment connection if it differs.

## Catalog browsing and client review

Select a generic **Subsonic/OpenSubsonic** server in the client, enter the
Lastbridge public URL, your local username, and the generated client password.
Link Last.fm through the browser first. Browser passwords do not work in clients.
The caller's profile now enables streaming and downloading. Restart or reconnect
the client after rebuilding so it reloads these capabilities.

Implemented read endpoints support XML/JSON, GET/form POST, and `.view` aliases:

- Public `getOpenSubsonicExtensions`: `apiKeyAuthentication` and `formPost` v1.
- Authenticated `ping`, `getLicense`, and the caller's own `getUser` profile.
  Administrative account management stays in the browser.
- `search3` (tag results), `search2` (folder-style album results), `getSong`,
  `getAlbum`, `getArtist`, `getMusicDirectory`, and `getCoverArt`.
- Saved-library enumeration: `getMusicFolders`, `getArtists`, `getIndexes`,
  `getAlbumList`/`getAlbumList2`, `getGenres`, `getStarred`/`getStarred2`,
  `getPlaylists`, and `getScanStatus`. Saved songs and their parent albums/artists
  populate these lists; genres and playlists remain empty for now. `getPlaylist`
  returns not-found. `star`/`unstar` mutate the caller's private favorites.

An empty/whitespace search query (also the literal `""` used by some clients)
returns the user's saved library. A nonempty query searches anonymous YouTube
Music metadata, even with an empty saved library. Searching does not save or
download tracks. Song results include official catalog audio; ordinary music
videos and unavailable tracks are excluded. This is interactive search, not
automatic matching of a requested recording to one result.

Each result type has independent count/offset pagination. Nonnegative counts
up to 2,147,483,647 are accepted. Nonempty remote searches clamp pages to 100;
empty-query saved-library sync accepts pages up to 500, with offsets across the
whole library so Feishin can continue syncing beyond 500 saved tracks. Remote
discovery is capped at the provider's first 100 candidates per type; remote
offsets beyond that window return an empty page. Short-lived per-user search
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
   failed operation. Playback review follows; favorite changes arrive later.

The mapping follows the [OpenSubsonic search contract](https://opensubsonic.netlify.app/docs/endpoints/search3/)
and [public extension discovery requirement](https://opensubsonic.netlify.app/docs/endpoints/getopensubsonicextensions/),
using [ytmusicapi browsing](https://ytmusicapi.readthedocs.io/en/stable/reference/browsing.html).
Automated protocol checks and real provider checks do not substitute for actual
client/device verification.

## YouTube feasibility probe

The probe runs independently of the service, with explicit network opt-in. Its
Python dependencies are now runtime dependencies; the empty `probe` group is kept
so earlier setup commands continue to work in the same local environment:

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
The service image now includes yt-dlp and minimal FFmpeg/ffprobe tools. It does
not include Node or Deno; playback uses the verified `visionos` path without
JavaScript. The developer probe may still request a local JavaScript runtime.

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
dependencies, without pytest, Ruff, uv, installed pip, or a C compiler. A separate
media build stage compiles only file-based MOV/Matroska input, AAC/Opus decoding,
MP4 remuxing without re-encoding, and null output for validation. It enables no network protocols or external media
libraries. Corresponding Debian source archives, build configuration, and LGPL
license are included under `/usr/local/share/lastbridge-media/`. The exact
configure recipe is in Dockerfile; update the source version and rebuild when
FFmpeg security updates are released. It is intentionally not a general-purpose
FFmpeg installation. Run development checks
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
Account pages, catalog browsing, audio playback, seeking, and downloads are
available, including favorites and permanent saves. Personalized recommendations
are still pending.

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
| Iteration 5 minimal playback image, OS/Python inventory | 0 | 44 |
| Iteration 5 FFmpeg source-version audit, separate | 0 | 16 |

The 44 high OS-inventory findings correspond to eight distinct CVEs across Debian
packages, with no fixed versions listed by this database. Lower-severity findings
also remain (58 medium, 61 low, one unknown). No vulnerabilities were reported for
the installed Python application dependencies, including cryptography and the
iteration 4 metadata dependencies. These scans reused the same database for
comparison.

The stock FFmpeg package trial pulled 292 OS packages and scanned at one critical
and 207 high findings, including libxml2. The final minimal build keeps the prior
87 OS packages and links its media tools only to libc/libm. Its uncompressed image
size is about 188 MB, versus 630 MB for the stock-package trial.

The OS/Python scan does **not** automatically inventory custom-built FFmpeg.
A separate CycloneDX source-version check used Debian FFmpeg
`7:7.1.5-0+deb13u1`, the exact patched source used by the build. It reported zero
critical, 16 high, 20 medium, and three low findings, with no fixed versions
listed. This conservatively checks the whole FFmpeg source package; applicability
to the enabled AAC/Opus/container features has not been fully assessed. Those
findings are additional to the OS inventory, and have not been suppressed.
Update and rebuild the pinned FFmpeg source as fixes become available.

These are dated scan results, not a guarantee of safety or a count of remotely exploitable application bugs. Other
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


Iteration 5, 2026-10-28: authenticated stream/download/HEAD, completed-file atomic
cache, shared acquisition, two-worker admission, range seeking, restart cleanup,
active response leases, disk/file limits, and configurable source AAC/Opus policy.
The existing 121-test suite plus 18 new playback tests pass (139 total). Real
host and disposable container checks acquired full recordings for the three
agreed songs in both codecs; client listening, iOS offline use, gapless playback,
and delivery from the deployment connection remain manual acceptance checks.
The user subsequently confirmed Feishin playback, seeking and downloads. Amperfy
testing is deferred. Permanent favorites follow in iteration 6; mixes and
scrobbles remain future work.

Iteration 6, 2026-10-29: private song/album/artist favorites, owned library
enumeration, persistent save jobs/retries, shared audio pins and permanent storage.
All 162 tests pass, including 23 new favorites/retention tests; lint, formatting,
diff and Compose configuration checks pass. A disposable Docker run saved full
AAC recordings for all three agreed songs, restarted the app, disabled provider
access, verified byte-identical downloads and range seeks, and preserved a second
user's retained file after the first user's unheart. The container used synthetic
accounts, temporary storage and UID 10001; real Feishin favorites and Amperfy
acceptance are still manual. No dependency or container media-tool changes in
this iteration; the earlier security findings remain a historical audit.

Iteration 7, 2026-10-29: durable private playback reports and Last.fm scrobble
delivery; automatic paginated loved-track imports, conservative matching,
pause/resume/refresh and local-removal protection. All 206 tests pass, including
44 sync tests covering account isolation, signatures/UTF-8, retries and ambiguous
delivery, interrupted requests, cursor recovery, matching, browser CSRF/escaping,
and schema 1/2/3 upgrades. Ruff lint/format, diff and Compose checks pass. A
disposable, network-disabled Docker run as UID 10001 verified installed-package
startup, private import, two-account mocked delivery, deduplication and restart.
All Last.fm responses were mocked; no live credentials, real scrobbles or real
Last.fm imports were used. Feishin server-scrobbling and live import acceptance
remain manual. Dependencies, audio policy and container media tools are unchanged;
no new vulnerability scan is claimed.


Iteration 8, 2026-10-29: private Comfort, Discover, Listen Again and Loved Tracks
playlists, bounded daily generation, per-user taste profiles, atomic snapshots,
outage/restart fallback and browser refresh controls. All 233 tests pass in
97.54s, including the regression for unhearting then rehearting an imported love.
Ruff lint/format (36 files), direct Python line-length scan (all src/tests lines
at most 79 characters), diff and Compose checks pass. The affected audio-worker
and catalog tests pass again after wrapping two pre-existing long strings
(51 tests, 14.43s). The existing TestClient/HTTPX deprecation warning remains.
A disposable installed Docker image passed with networking disabled as UID10001:
schema 5, two-user playlist isolation, strict matching against mocked metadata,
UTF-8/XML responses, no automatic audio pins and interrupted-refresh recovery.
Real recommendation coverage, latency and client presentation remain manual review
items. No live credentials, existing data volumes or actual Last.fm requests were
used; no new security scan was performed.
