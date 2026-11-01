"""Explicit construction and single-owner resource lifecycle."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from tempfile import TemporaryFile

import httpx
from starlette.concurrency import run_in_threadpool

from lastbridge.accounts import Accounts
from lastbridge.audio import AudioCache
from lastbridge.config import Settings
from lastbridge.features.catalog.adapters.http.artwork import ArtworkLookup
from lastbridge.features.catalog.adapters.sqlite.repository import (
    CatalogRepository,
)
from lastbridge.features.catalog.adapters.youtube.documents import (
    MetadataDocuments,
)
from lastbridge.features.catalog.adapters.youtube.provider import (
    MetadataProvider,
)
from lastbridge.features.catalog.application.ports import MetadataPort
from lastbridge.features.catalog.application.service import Catalog
from lastbridge.features.library.adapters.sqlite.repository import (
    LibraryRepository,
)
from lastbridge.features.library.application.service import Library
from lastbridge.features.playback.adapters.youtube.provider import (
    AudioProvider,
)
from lastbridge.features.playback.application.ports import (
    AudioProvider as AudioProviderPort,
)
from lastbridge.lastfm import Lastfm
from lastbridge.lyrics import Lyrics
from lastbridge.mixes import Mixes
from lastbridge.platform.credentials import Credentials
from lastbridge.platform.database import Database
from lastbridge.platform.migrations import migrate
from lastbridge.playlists import Playlists
from lastbridge.radio import Radio
from lastbridge.sync import LastfmSync


@dataclass(frozen=True)
class Providers:
    metadata: MetadataPort | None = None
    audio: AudioProviderPort | None = None


@dataclass(frozen=True)
class Services:
    database: Database
    credentials: Credentials
    accounts: Accounts
    catalog: Catalog
    library: Library
    audio: AudioCache
    http_client: httpx.AsyncClient
    lastfm: Lastfm
    lyrics: Lyrics
    sync: LastfmSync
    mixes: Mixes
    radio: Radio
    playlists: Playlists
    password_limit: asyncio.Semaphore


def account_storage(config: Settings):
    credentials = Credentials(
        config.data_dir, config.data_dir / "lastbridge.sqlite3"
    )
    database = Database(config.data_dir)
    migrate(database, credentials)
    return (
        database,
        credentials,
        Accounts(database=database, credentials=credentials),
    )


@asynccontextmanager
async def bootstrap(
    config: Settings,
    *,
    start_workers: bool = True,
    providers: Providers | None = None,
) -> AsyncIterator[Services]:
    selected = providers if providers is not None else Providers()
    try:
        for directory in (
            config.data_dir,
            config.data_dir / "cache",
            config.data_dir / "saved",
        ):
            directory.mkdir(parents=True, exist_ok=True)
            with TemporaryFile(dir=directory):
                pass
    except OSError:
        raise RuntimeError(
            "Cannot initialize writable Lastbridge storage"
        ) from None

    async with AsyncExitStack() as resources:
        try:
            database, credentials, accounts = await run_in_threadpool(
                account_storage, config
            )
            repository = CatalogRepository(database)
            catalog = Catalog(
                repository,
                selected.metadata
                if selected.metadata is not None
                else MetadataProvider(),
                MetadataDocuments(repository),
                ArtworkLookup(repository),
            )
            library = Library(
                LibraryRepository(database, config.audio_policy),
                catalog,
                config.audio_policy,
            )
            lastfm = Lastfm(
                config.lastfm_api_key.get_secret_value(),
                config.lastfm_api_secret.get_secret_value(),
            )
        except Exception:
            raise RuntimeError(
                "Cannot initialize account storage/credentials"
            ) from None

        try:
            audio = AudioCache(
                config.data_dir / "cache" / "audio",
                budget=config.audio_cache_bytes,
                maximum=config.audio_file_max_bytes,
                reserve=config.audio_free_reserve_bytes,
                policy=config.audio_policy,
                provider=selected.audio
                if selected.audio is not None
                else AudioProvider(),
                saved_directory=config.data_dir / "saved" / "audio",
                pins=library.pins(),
            )
        except Exception:
            raise RuntimeError("Cannot initialize audio cache") from None
        resources.push_async_callback(audio.close)
        resources.push_async_callback(library.close)
        await library.start(audio, worker=start_workers)

        client = await resources.enter_async_context(
            httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                limits=httpx.Limits(
                    max_connections=20, max_keepalive_connections=10
                ),
                follow_redirects=False,
            )
        )
        radio = Radio(accounts, catalog, library, lastfm=lastfm, client=client)
        resources.push_async_callback(radio.close)
        lyrics = Lyrics(accounts, catalog, client)
        resources.push_async_callback(lyrics.close)
        sync = LastfmSync(accounts, catalog, library, lastfm, client)
        resources.push_async_callback(sync.close)
        mixes = Mixes(
            accounts,
            catalog,
            library,
            lastfm,
            client,
            refresh_interval=config.mix_refresh_hours * 3600,
            inactive_ttl=config.mix_inactive_days * 86400,
        )
        resources.push_async_callback(mixes.close)
        playlists = Playlists(accounts, catalog, library, mixes, radio)
        if start_workers:
            await sync.start()
            await mixes.start()
        yield Services(
            database=database,
            credentials=credentials,
            accounts=accounts,
            catalog=catalog,
            library=library,
            audio=audio,
            http_client=client,
            lastfm=lastfm,
            lyrics=lyrics,
            sync=sync,
            mixes=mixes,
            radio=radio,
            playlists=playlists,
            password_limit=asyncio.Semaphore(2),
        )
