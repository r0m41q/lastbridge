# Lastbridge

Last.fm discovery and a private saved library through existing OpenSubsonic music
clients. Python service, with YouTube Music as the first music provider.

**Current scope: accounts, Last.fm linking, catalog browsing, audio playback,
seeking/downloads, private favorites, shared permanent audio retention,
Last.fm scrobbling, resumable loved-track imports, personal mixes, editable
playlists with optional downloads, radio continuation, and synchronized lyrics.** The user confirmed
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
authentication, form POST, and structured song lyrics version 1.

`/rest/ping` and `/rest/ping.view` support GET and URL-encoded form POST, XML
(default) and JSON (`f=json`). Supply `v` and `c`. Errors use protocol responses:
40/44 for bad credentials, 43 for conflicting mechanisms, 50 for missing initial
Last.fm linking, and 0 for unimplemented endpoints. Music clients require an
initial link; revoked Last.fm authorization pauses delivery while preserving
local playback. Administration remains accessible before linking.

SQLite schema version 8 is created transactionally on first startup; versions
1–7 upgrade automatically without changing accounts, credentials, catalog IDs,
favorites, sync progress, mixes, radio snapshots or cached lyrics. Back up the
stopped data volume before upgrading. Newer schema
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

To preserve downloaded favorites and playlists as well as membership, include `data/saved/`
with its audio files and manifests in that stopped-volume backup. Restoring only
SQLite and the credential key preserves save intent but requires reacquisition of
missing audio from the provider. Older application versions cannot open schema 8;
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
Downloaded playlists are available through `/playlists`. Per-user size
accounting and fuller admin controls remain later iterations.

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

For review, back up the stopped data volume before upgrading to schema 8, then
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

## Artist top songs and radio review correction

Feishin has separate **Favorite songs** (your hearts) and **Top songs** sections.
The latter's **Community** tab now calls `getTopSongs`, accepting the artist name
or catalog artist ID (`topSongsByArtistId` version 1). It uses YouTube Music's
artist popular-song shelf, including recordings you have never saved. This is
provider popularity, not a personal Last.fm ranking. ID takes precedence over
name. Name-only requests require one exact case-insensitive artist match and
reject ambiguous channel names. The response preserves provider order, removes
duplicate/unavailable/video entries, caps at 50, and applies only your own heart
and play-count decoration. It never queues downloads or favorites. Count zero
and HEAD make no upstream requests. Missing artist shelves return an empty list.

Public top-song metadata caches for 15 minutes across at most 32 artists. Opening
a remote artist warms this metadata alongside the existing album listing. After
restart or expiry it can be fetched again; a provider failure can reuse the
previous entry for up to 24 hours after expiry. No new key, schema migration or
paid provider is required. Available provider duration strings become seconds;
unknown durations remain unknown. The artist's **Personal** tab and **Favorite
songs** still reflect your own listening/hearts. Feishin's local favorite/personal
views can fetch every artist album; we cannot redefine those client tabs as
remote popularity without changing their meaning.

A regression reproduced radio failing its one-second metadata admission limit
when both browsing slots were occupied. Radio now has two separate metadata
slots alongside the two browsing slots, matching its existing two-fetch bound.
Maximum metadata subprocess concurrency is therefore four; individual response
size/time limits and bounded radio task admission remain. This avoids starvation
from album browsing while keeping provider work bounded. Fixed-category warning
logs identify capacity, deadlines, provider failures or an entirely filtered
pool without logging user IDs, credentials or provider URLs.

Feishin **Start radio** consumes the returned similar-song array directly into
its queue; no browser action is required. The server also updates its one stable
private Radio fallback playlist, rather than creating a new list for each click.
Saved/downloaded status does not gate radio. The current batch has up to 50 tracks;
continuous automatic refill depends on the client. The server cannot push an
unrequested queue change into a Subsonic client. Explicit removals and 24-hour
recent-play exclusions can exhaust a pool; this is now identifiable in logs.

For review, rebuild with `docker compose up -d --build`, reconnect Feishin to
refresh extensions, and open a remote artist's **Top songs → Community**. Try
**Start radio** on the agreed Daft Punk, Joji and OTOY recordings directly in
Feishin, including a track not kept downloaded. Repeat while an artist page
loads. If a click still does nothing, record the exact song and the fixed-category
`Radio could not complete` log. Do not share generated client passwords or URLs
containing credentials. These changes fix a reproduced contention path; they do
not establish that every reported deployed-client failure has the same cause.

## Editable and downloaded playlists

Create, rename, add/remove tracks, reorder and delete private playlists in a
supporting music client. Lastbridge implements the OpenSubsonic
[create/replace](https://opensubsonic.netlify.app/docs/endpoints/createplaylist/),
[update](https://opensubsonic.netlify.app/docs/endpoints/updateplaylist/) and
[delete](https://opensubsonic.netlify.app/docs/endpoints/deleteplaylist/)
contracts. GET and URL-encoded form POST, `.view`, XML and JSON work. The user's
profile advertises `playlistRole=true`. Generated mixes and radio return
`readonly=true`; local lists return `readonly=false`. All lists stay private;
requesting `public=true` returns error 50. Another user's list returns error 70,
including for administrators. Sharing and external playlist imports are later
work.

Entries preserve order and duplicates. `createPlaylist` with `playlistId`
replaces all entries atomically; omitting `songId` clears the list. Existing
name/comment/retention policy remain unless an applicable field changes.
`updatePlaylist` removes zero-based positions from the original list, then
appends additions in supplied order. Repeated removal indices are applied once.
Unknown/non-song IDs, invalid positions or an oversized result reject the whole
edit. Names are 1–200 characters with non-whitespace content; comments allow up
to 2,000 characters. XML-invalid controls are rejected. Lists support up to
1,000 entries and 200 lists per user. Playlist mutation forms support up to
2,048 fields/256 KiB; use form POST for large lists to avoid client/proxy URL
length limits. Unrelated endpoints retain their smaller input limits.

An ordinary playlist stores metadata only: it creates no favorite, download job,
or permanent audio pin. Open `/playlists` from your account page to create an
empty list or copy any of your local/generated lists into an independent editable
snapshot. Copying a mix or radio preserves its currently visible track order;
future source refreshes or radio selections do not change the copy. Copies start
with **Keep downloaded** off, including copies of a kept list. A source with more
than 1,000 entries must be narrowed in the client before copying.

Choose **Keep downloaded** in the browser to queue permanent server copies of
all tracks; later additions are included and removed tracks release their pins.
This does not heart tracks or download them onto a phone. Kept tracks appear in
your saved-library enumeration, even when they have no heart. Download status,
failures and retry are on `/library`. Saved files remain outside the 1 GB
temporary-cache budget; there is no application storage quota. Free disk space
and the existing source-file/acquisition limits still apply.

Favorites and kept playlists across all users share one recording/rendition and
save job. Duplicate entries create one pin requirement. Turning off retention or
deleting a list releases only its requirement; audio remains saved while another
favorite or kept list needs it. After the last requirement and active transfer
lease end, audio returns to the temporary cache and can be evicted. Playlist
track IDs and already loaded client queues remain usable. Retention and job
intent commit together and restart reconciliation recovers interrupted work.
Deletion removes the list and entries; releasing downloads preserves the list.

Feishin 1.17.0 source was inspected: it creates an empty list, appends/removes
tracks through `updatePlaylist`, and replaces entries through `createPlaylist`
for one reorder path. Another reorder path makes two separate remove/add
requests. Each server request is atomic, but those two client requests are not
one transaction; an interrupted client reorder can leave a partial list. Keep a
copy of an important list before using such a client operation. Actual UI
acceptance in Feishin and iOS clients remains manual.

For review:

1. Back up the stopped complete data volume (database, `credentials.key`, saved
   audio and manifests) before the schema 8 upgrade. Run
   `docker compose up -d --build` and reconnect Feishin to refresh permissions.
2. Create a list in Feishin, add the agreed Daft Punk/Joji/OTOY recordings, change
   their order, rename it, remove an entry and reload. Check order after restart.
   Confirm ordinary creation does not queue downloads on `/library`.
3. In `/playlists`, copy a generated mix/radio. Edit the copy in Feishin and
   confirm the generated source remains read-only and unchanged.
4. Keep a local list downloaded, check `/library`, add/remove tracks, then replay
   and seek after completion/restart. Confirm hearts were not added. Release it
   and confirm its track list remains.
5. Keep the same track with a second account or a separate favorite. Release or
   delete the first playlist while it plays. The other retention and playback
   must survive; neither account can view or edit the other's lists.

## Personal mix playlists

After linking Last.fm, open **Playlists** in your music app. Lastbridge exposes
four private, server-managed, read-only lists through
[getPlaylists](https://opensubsonic.netlify.app/docs/endpoints/getplaylists/) and
[getPlaylist](https://opensubsonic.netlify.app/docs/endpoints/getplaylist/).
Playlist IDs stay stable across refreshes and restarts. Each user's IDs and
contents are separate; even administrators cannot read another user's mixes
through these endpoints. Editable private playlists appear alongside these
generated lists; see **Editable and downloaded playlists** below.

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
by a client. Custom client home-screen sections remain outside the standard API.

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

## Radio and queue continuation

Use a music client's song/album/artist radio action when available. Lastbridge
supports [getSimilarSongs](https://opensubsonic.netlify.app/docs/endpoints/getsimilarsongs/)
and [getSimilarSongs2](https://opensubsonic.netlify.app/docs/endpoints/getsimilarsongs2/)
with JSON, XML, form POST and `.view` routes. Both accept catalog song, album and
artist IDs; accepting song/album IDs on the second endpoint is an additional
compatibility convenience. `count` defaults to 50 and is capped at 50; zero returns
no songs, malformed/negative values fail. HEAD cannot select radio.

The legacy protocol describes Last.fm artist similarity. This implementation uses
YouTube Music's anonymous
[watch radio](https://ytmusicapi.readthedocs.io/en/stable/reference/watch.html)
to continue a recording, while Last.fm taste still drives the personal mixes.
Songs seed directly. Albums use their first available track; artists use a known
catalog track, or the first available track on their first returned release.
Artist/album hydration is bounded by the same request deadline. An unavailable
seed fails rather than silently starting unrelated radio.

Only available official album audio is accepted. Video/unknown-type rows,
malformed metadata and tracks of unknown/short duration are omitted; official
album recordings with a live/remix title retain that version. Duplicate recording
identities and IDs, the seed itself, local removals, and this user's local
starts/completed reports within the last 24 hours are excluded. A start helps
avoid queue repetition without counting as a completed listen. This filter does
not fetch the full remote Last.fm listening history. Different users can receive
different results from the same public recommendation pool.

Public metadata pools contain at most 50 songs, with a 15-minute lifetime and a
32-seed LRU bound. Fetches are shared per source, at most four pending and two
running, using the existing bounded metadata subprocess. The entire request has
a 20-second deadline, including artist/album hydration. Exhausted cached pools
can fetch another radio after one minute; no recently played song is reinserted
just to fill the count. A short result or unavailable-radio response is possible.
These are continuation batches, not a guarantee of infinite playback in every app.

Starting a stream or reporting `submission=false` warms radio metadata without
waiting for it. Warming is limited to once a minute per user (128-user LRU),
shares the above limits, and never downloads speculative audio, records a play,
creates favorites, or changes the selected radio playlist. Downloads and HEAD
requests do not warm radio. No audio prefetch is enabled.

After an explicit successful radio request, **Radio · starting song** appears as
an additional private, read-only playlist. Its ID is stable, and only the user's
latest selection is retained in schema 6. Open `/radio` from `/mixes` to start it
from saved or personal-mix songs when the music client has no radio control.
The browser action requires a session/CSRF token. It changes the server playlist,
not an already loaded client queue. Recent plays/removals are filtered again when
the playlist is read; it does not refill itself without another radio request.

The snapshot survives restart. Provider outages can reuse the same user's last
successful snapshot for that seed, with current exclusions applied. A different
seed is never silently substituted. Snapshots bind the linked Last.fm identity;
switching identity hides the old list and prevents in-flight old publication.
Existing snapshots remain readable after a relink is requested. Provider tokens,
links, counterparts and credentials are never stored in the radio snapshot.

Host measurements on 2026-10-29 used anonymous public access, disposable storage,
and the agreed three complete AAC recordings. Each radio returned 49 eligible
tracks after excluding the seed. No live Last.fm credentials were used.

| Recording | Cold radio metadata | Cached radio | Cold complete audio | Cached audio |
| --- | ---: | ---: | ---: | ---: |
| Daft Punk — Instant Crush | 1.412 s | 0.004 s | 2.827 s | <0.001 s |
| Joji — PIXELATED KISSES | 1.076 s | 0.004 s | 2.174 s | <0.001 s |
| OTOY — Судоми | 1.275 s | 0.004 s | 2.066 s | <0.001 s |

Audio timing includes acquisition and complete decode validation before the
server can deliver bytes. It does not measure audible client buffering, gapless
transition or deployment-network latency. Metadata timing measures server radio
selection; metadata warming can remove that work from a later continuation
request. These measurements justify retaining metadata-only warming for now,
without promising that every next track starts instantly.

Manual acceptance after a stopped-volume backup and `docker compose up -d --build`:

1. In Feishin, start a personal mix or search result, play and seek. Use its radio
   action and check related tracks load without the seed or recent repetitions.
2. Heart a continuation, check `/library` reaches saved, and replay it. Enable
   server scrobbling, listen past the threshold, and check `/lastfm/sync` plus your
   Last.fm history for one report. Disable any duplicate direct scrobbler.
3. Open `/radio` in the browser, select a starting song, and load the resulting
   Radio playlist in the client. Restart and check it persists. Another account
   must have its own selection and exclusions.
4. Record first audio and next-track delay on the actual deployment/client;
   report which autoplay controls work. Amperfy testing remains explicitly
   deferred, and no automatic iOS/Feishin UI acceptance is claimed here.

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

## Synchronized lyrics

`getOpenSubsonicExtensions` advertises `songLyrics` version 1.
Authenticated `getLyricsBySongId` accepts a catalog song `id`, using GET or form
POST with XML or JSON responses. Timed lines use integer milliseconds; XML puts
line text inside `<line start="...">text</line>`, while JSON uses `value`.
The server prefers timed lines, returns plain text with `synced=false` when only
plain lyrics are usable, and returns an empty `structuredLyrics` array for
missing matches, instrumental tracks or provider failures. Playback is independent
of lyric retrieval. Unknown/non-song IDs return error 70; missing IDs return 10.
HEAD validates the ID without contacting the provider. The legacy artist/title
`getLyrics` endpoint and word-level karaoke are not implemented.

Lyrics come from [LRCLIB](https://lrclib.net/docs), without an API key, using its
track-signature endpoint. Lastbridge sends public artist, title, album and
duration metadata only. It checks all four returned fields, permitting Unicode
case/whitespace normalization and at most two seconds of duration difference.
Version qualifiers, punctuation and accents are retained; there is no fuzzy
search or substitution of a studio recording for a live/remix version. Differences
in credits, release names or provider spelling can therefore leave lyrics absent.
Matching metadata is a safeguard, not proof of perfect community-authored timing.
Language is `und` because the source does not supply a reliable language field.

The LRC parser supports fractional timestamps, repeated timestamps, metadata tags
and signed millisecond offsets. It applies offsets to timestamps and returns
`offset=0`, orders lines by time, retains blank timed cues, rejects malformed or
out-of-duration timing, and never derives timestamps from plain text. Malformed
synchronized text can fall back to valid plain text for the matched recording.

SQLite caches public structured results across users and restarts, separate from
favorites/audio pins. Successful lyrics expire after seven days; missing or
rejected matches after one day; transient failures after one minute. An expired
positive result may be used on provider failure for up to 30 days from its last
successful fetch. Changed song metadata invalidates its fingerprint. The cache
retains at most 1,000 songs; it is outside the audio-cache byte budget. Responses
are limited to 256 KiB, with bounded text and at most 2,000 expanded lines.

Requests for the same song/metadata share work. At most four fetches may be
pending, with one provider request at a time, a 250 ms gap and an eight-second
fetch/queue deadline. HTTP 429/503 honors numeric or HTTP-date `Retry-After`
server-wide, with a minimum one-minute cooldown; absent/invalid headers use one
minute. Redirects are refused. A disconnected client does not cancel shared work;
shutdown cancels and drains pending requests. No lyrics are fetched at startup,
when generating mixes, or merely by streaming audio.

Live host check on 2026-10-29 used anonymous YouTube metadata for the three agreed
source IDs, disposable SQLite storage and LRCLIB. No audio was downloaded and no
Last.fm credentials were used. No lyric text is included in these measurements.

| Recording | Accepted result | Lines | First request | Cache hit |
| --- | --- | ---: | ---: | ---: |
| Daft Punk — Instant Crush | Synchronized | 83 | 0.176 s | 0.001 s |
| Joji — PIXELATED KISSES | Synchronized | 18 | 0.069 s | 0.001 s |
| OTOY — Судоми | Unavailable | 0 | 0.571 s | 0.001 s |

These are observations of current provider coverage, not a guarantee for other
releases or future availability. Feishin's structured-lyrics parser was inspected;
actual display and audible synchronization have not been verified by the agent.

Manual review after backing up the stopped data volume (schema 8 upgrade) and
running `docker compose up -d --build`:

1. Reconnect Feishin so it refreshes extension discovery. Prefer server lyrics
   and disable automatic internet lyric lookup during this check, so an external
   client fallback does not mask the server result.
2. Search/play the agreed Daft Punk and Joji recordings, open lyrics, and verify
   line highlighting. Seek forward and backward; compare the words to the audio.
3. Check OTOY still plays with no server lyrics. Other releases may have coverage.
4. Repeat the listening session: mix/radio, play, seek, lyrics, heart, saved replay
   and one server scrobble. Confirm saved membership remains private per account.
5. Test Amperfy when the iOS device is available; its display/seek alignment remains
   deferred. Report title/release and timing differences rather than changing to
   an unrelated lyric match.

## Catalog browsing and client review

Select a generic **Subsonic/OpenSubsonic** server in the client, enter the
Lastbridge public URL, your local username, and the generated client password.
Link Last.fm through the browser first. Browser passwords do not work in clients.
The caller's profile now enables streaming and downloading. Restart or reconnect
the client after rebuilding so it reloads these capabilities.

Implemented read endpoints support XML/JSON, GET/form POST, and `.view` aliases:

- Public `getOpenSubsonicExtensions`: `apiKeyAuthentication`, `formPost` and
  `songLyrics` v1 and `topSongsByArtistId` v1. Authenticated
  `getLyricsBySongId` serves structured lyrics.
- Authenticated `ping`, `getLicense`, and the caller's own `getUser` profile.
  Administrative account management stays in the browser.
- `search3` (tag results), `search2` (folder-style album results), `getSong`,
  `getAlbum`, `getArtist`, `getMusicDirectory`, and `getCoverArt`.
- Saved-library enumeration: `getMusicFolders`, `getArtists`, `getIndexes`,
  `getAlbumList`/`getAlbumList2`, `getGenres`, `getStarred`/`getStarred2`,
  `getPlaylists`, and `getScanStatus`. Saved songs and their parent albums/artists
  populate these lists; genres remain empty. `getPlaylist` returns an owned
  local playlist or generated mix/radio. `star`/`unstar` mutate private favorites.
- Playlist mutations: `createPlaylist` (create or full replacement),
  `updatePlaylist` (rename, comment, remove indices, append tracks), and
  `deletePlaylist`. HEAD never mutates playlists.

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


Iteration 9, 2026-10-29: song/album/artist radio, private recent-play exclusions,
bounded metadata warming, durable latest-radio playlist and browser fallback.
All 255 tests pass in 104.44s, including 22 radio cases and a complete simulated
play/seek/continue/heart/save/replay/scrobble session. Ruff lint/format (39 files),
direct Python line-length and fixture-import checks, diff and Compose checks pass.
Live anonymous YouTube radio and all three agreed full AAC files succeeded using
disposable storage; measurements are above. A network-disabled installed Docker
image also passed as UID10001 with synthetic accounts and mocked providers:
schema6, radio/seek/save/replay/report dedup, private filtering and restart.
No live Last.fm credentials or writes, existing data volume, or user service
restart was used. Real client radio/autoplay and audible next-track delay remain
manual checks; Amperfy deferred. No audio prefetch or new security scan.


Iteration 10, 2026-10-29: LRCLIB recording-signature matching, bounded persistent
structured lyrics cache, line-level OpenSubsonic extension, JSON/XML/form POST,
plain fallback and graceful empty responses. All 291 tests pass in 113.78s,
including 36 new lyrics cases and the repeated simulated listening session with
lyrics, radio, seek, heart/save/replay and mocked accepted scrobble delivery.
Schema 1–6 migration tests preserve prior state; newer databases are refused.
Ruff lint/format (42 files), direct 79-character line and fixture-import scans,
diff and Compose checks pass. The existing TestClient/HTTPX deprecation remains.
Live anonymous metadata/LRCLIB checks accepted timed lyrics for the agreed Daft
Punk and Joji recordings; OTOY had no accepted match. Measurements are above.
A disposable installed Docker image passed with networking disabled as UID10001:
schema7, lyrics JSON/XML/cache/restart, radio, seek, saved replay, report dedup and
private filtering. Runtime dependencies, lock, Dockerfile and Compose unchanged;
no new vulnerability scan. No real Last.fm requests/writes, user credentials,
existing volumes or running service were used. Feishin and Amperfy display and
actual audible lyric alignment remain manual review steps.


Iteration 11, 2026-10-29: private editable playlists, ordered duplicate entries,
full replacement and indexed edits, generated-list copies, browser retention
controls, and shared favorite/playlist pins. Schema 8 adds local playlists and a
retention view; versions 1–7 migrate while preserving earlier state. Ordinary
playlists create no audio jobs. Kept tracks join saved-library browsing and the
existing download status/retry worker without gaining hearts. Sharing remains
unsupported. Copies are independent snapshots. Limits: 200 local lists per user,
1,000 entries per list. A reproduced overlapping-reconciliation/cancelled-delete
race is fixed by rereading durable retention membership after storage locking.

Validation: **316 tests pass** (127.04 s), including 25 new playlist cases for
ordering/duplicates/XML/form POST, atomic failures/limits, ownership/read-only
lists/HEAD/authentication, copy privacy/CSRF, cross-user/favorite retention,
editing kept lists, release during acquisition and active leases, failed-job
retry, restart, migration, cancellation and stale-reconciliation recovery. One
existing Starlette TestClient/HTTPX deprecation remains. Ruff lint and formatting
pass across 45 Python files; every source/test line is at most 79 characters.
Direct imported test helper/fixture parameter-shadow checks, diff and Compose
validation pass. Flake8 is not installed, so no Flake8 run is claimed.

Installed image `lastbridge-iteration11` built successfully with unchanged locked
dependencies and media layers. A network-disabled disposable container runs as
UID 10001 and passes schema 8, editable playlist ordering/duplicates/ownership,
browser copy/keep/release, union pins/restart, lyrics JSON/XML, radio, range seek,
saved replay and playback-report deduplication checks. Synthetic accounts and
mocked metadata/audio/lyrics only; no existing data volume or running service was
used. No real Last.fm or live audio-provider acceptance is claimed. No new
security scan was run; earlier base/media scan limitations still apply. Actual
Feishin playlist UI and iOS acceptance remain manual using the steps above.


Iteration 11 review correction, 2026-10-29: added remote artist top songs and the
artist-ID extension; isolated two radio metadata slots from two browsing slots;
added safe radio failure categories. The slot-starvation regression failed before
the fix and passes afterwards. Full suite:326 passed (124.00 s); subsequent focused
artist/radio run:32 passed (9.42 s), same existing TestClient warning. No new schema,
keys or dependencies. Live metadata-only checks using disposable synthetic account
storage returned five Daft Punk top songs (0.865 s), and cold authenticated radio
batches of48/48/49 songs for Daft Punk/Joji/OTOY (1.161/1.345/1.329 s); cached
requests took0.005 s each. No audio or retention was created. Deployed-container
provider-only checks also succeeded for all three seeds. These results do not
establish the exact cause of every Feishin click failure; rebuilt-client retest
remains necessary. Still reviewing iteration11; external imports have not started.


Review image `lastbridge-iteration11-review` also passes a disposable network-none
smoke as UID10001: artist top songs, existing playlist retention/restart/privacy,
lyrics, radio, range seeking, saved replay and report deduplication. Ruff lint,
formatting (46 files), direct 79-character and fixture-shadow scans, diff and
Compose validation pass. Existing running service and user storage were not
changed. Rebuild/reconnect and confirm the actual Feishin behavior before treating
this review correction as client-accepted.
