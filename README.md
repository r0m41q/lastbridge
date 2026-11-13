# Lastbridge

A self-hosted OpenSubsonic server that combines Last.fm recommendations and YouTube Music into your Subsonic client. Listen in your music app; manage accounts, imports, and server settings in the web UI.

- Search and play artists, albums, and songs.
- Personal mixes based on user's Last.fm listening.
- Sync song likes and scrobbles with Last.fm.
- Import YouTube and your Last.fm playlists, with daily refreshes for Last.fm and shared charts.
- Keep favorites and playlists downloaded for use with Jellyfin.
- Synced lyrics.
- Internet radio.

Each user has their own library and Last.fm account.

## Get started

Copy the example configuration:

```sh
cp .env.example .env
```

Edit `.env`:

- Set `LASTBRIDGE_PUBLIC_URL` to the URL you will use to open Lastbridge.
- Add your `LASTBRIDGE_LASTFM_API_KEY` and `LASTBRIDGE_LASTFM_API_SECRET` from
  [Last.fm](https://www.last.fm/api/account/create). Set the callback URL to  `<LASTBRIDGE_PUBLIC_URL>/lastfm/callback`.
- For access from other devices, set `LASTBRIDGE_HOST=0.0.0.0`.

Start the server and create your administrator account:

```sh
docker compose up -d --build
docker compose exec lastbridge /app/venv/bin/python -m lastbridge.manage bootstrap-admin admin
```

Open your configured URL, sign in, and link Last.fm. Generate a **client password** on the account page, then connect your music app with the server URL, your username, and that password. Your browser password is separate.

Enable server scrobbling in your music app to send listens to Last.fm.
Administrators can add other users from **Administration**.

## Using Lastbridge

Like a song to save it and sync the like to Last.fm. Choose **Keep downloaded** on a playlist to save its audio on the server too. Normal playlists don't retain audio. Use **Choose recording** in Saved library to replace a wrong or unavailable track.

Use **Playlists** in the web UI to import a public YouTube playlist or one of your Last.fm playlists by URL or numeric ID. Last.fm imports use your linked account, stay private and refresh daily. Review unmatched tracks before adding it. Administrators can share YouTube charts with everyone and manage import jobs and internet radio stations from **Administration**.

Audio providers, format, and cache limits are also in **Administration**. The first play of an uncached song may take a few seconds to start.

## Storage and Jellyfin

Storage defaults to `./data`. To use separate disks, set `LASTBRIDGE_DATA_DIR_HOST` for the database and cache, and `LASTBRIDGE_SAVED_AUDIO_DIR_HOST` for saved music. Both directories must be writable by the container's user, UID `10001`.

Point Jellyfin at the saved music directory; Lastbridge writes audio tags, album covers, and lyrics there. Use **Saved file** in Saved library to repair tags and fill missing artwork or lyrics without downloading again. Optional scan notifications are configured in [.env.example](.env.example).

Before upgrading, stop Lastbridge and back up the data and saved music directories together, including `credentials.key`.

## Update

After pulling the latest changes:

```sh
docker compose up -d --build --force-recreate
```
