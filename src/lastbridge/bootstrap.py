import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from tempfile import TemporaryFile

import httpx
from starlette.concurrency import run_in_threadpool

from lastbridge.config import Settings
from lastbridge.features.accounts.adapters.sqlite.repository import (
    AccountRepository,
)
from lastbridge.features.accounts.application.service import Accounts
from lastbridge.features.administration.adapters.filesystem.usage import (
    StorageUsage,
)
from lastbridge.features.administration.adapters.sqlite.repository import (
    DashboardRepository,
)
from lastbridge.features.administration.application.service import (
    Administration,
)
from lastbridge.features.catalog.adapters.http.artwork import ArtworkLookup
from lastbridge.features.catalog.adapters.sqlite.repository import (
    CatalogRepository,
)
from lastbridge.features.catalog.adapters.subsonic.documents import (
    client_document,
)
from lastbridge.features.catalog.adapters.youtube.documents import (
    MetadataDocuments,
)
from lastbridge.features.catalog.adapters.youtube.provider import (
    MetadataProvider,
)
from lastbridge.features.catalog.application.ports import MetadataPort
from lastbridge.features.catalog.application.service import Catalog
from lastbridge.features.discovery.adapters.sqlite.repository import (
    MixRepository,
)
from lastbridge.features.discovery.application.service import Mixes
from lastbridge.features.lastfm_sync.adapters.sqlite.repository import (
    SyncRepository,
)
from lastbridge.features.lastfm_sync.application.service import LastfmSync
from lastbridge.features.library.adapters.sqlite.repository import (
    LibraryRepository,
)
from lastbridge.features.library.application.service import Library
from lastbridge.features.lyrics.adapters.lrclib.provider import Lrclib
from lastbridge.features.lyrics.adapters.sqlite.repository import (
    LyricsRepository,
)
from lastbridge.features.lyrics.application.service import Lyrics
from lastbridge.features.playback.adapters.filesystem.metadata import (
    MetadataWriter,
)
from lastbridge.features.playback.adapters.filesystem.storage import AudioFiles
from lastbridge.features.playback.adapters.http.response import AudioResponse
from lastbridge.features.playback.adapters.sqlite.repository import (
    MediaFileRepository,
)
from lastbridge.features.playback.adapters.youtube.provider import (
    AudioProvider,
)
from lastbridge.features.playback.application.media import MediaPlacement
from lastbridge.features.playback.application.ports import (
    AudioProvider as AudioProviderPort,
)
from lastbridge.features.playback.application.service import AudioCache
from lastbridge.features.playback.domain.models import MediaDescriptor
from lastbridge.features.playlists.adapters.sqlite.imports import (
    ImportRepository,
)
from lastbridge.features.playlists.adapters.sqlite.repository import (
    PlaylistRepository,
)
from lastbridge.features.playlists.adapters.youtube.imports import (
    PlaylistProvider,
)
from lastbridge.features.playlists.application.imports import Imports
from lastbridge.features.playlists.application.service import Playlists
from lastbridge.features.radio.adapters.catalog import RadioCatalog
from lastbridge.features.radio.adapters.sqlite.repository import (
    RadioRepository,
)
from lastbridge.features.radio.application.service import Radio
from lastbridge.platform.credentials import Credentials
from lastbridge.platform.database import Database
from lastbridge.platform.migrations import migrate
from lastbridge.providers.lastfm.client import Lastfm
from lastbridge.providers.lastfm.session import LastfmSession


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
    administration: Administration


def account_storage(
    config: Settings,
) -> tuple[Database, Credentials, Accounts]:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    credentials = Credentials(
        config.data_dir, config.data_dir / "lastbridge.sqlite3"
    )
    database = Database(config.data_dir)
    migrate(database, credentials)
    return (
        database,
        credentials,
        Accounts(AccountRepository(database), credentials),
    )


def catalog_media_descriptor(catalog, source, policy):
    stored = catalog.read_source(source, "song")
    document = stored["document"]
    return MediaDescriptor(
        source=source,
        policy=policy,
        title=document.get("title") or source,
        artist=document.get("artist") or "Unknown Artist",
        album=document.get("album"),
        album_artist=document.get("artist"),
        track_number=document.get("track"),
        year=document.get("year"),
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
            repository = CatalogRepository(database, client_document)
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
            media_files = None
            media_placement = None
            metadata_writer = None
            descriptor_factory = None
            if providers is None:
                media_files = MediaFileRepository(database)
                media_placement = MediaPlacement(media_files)
                metadata_writer = MetadataWriter()

                def make_descriptor(source, policy):
                    return catalog_media_descriptor(catalog, source, policy)

                descriptor_factory = make_descriptor
            storage = AudioFiles(
                config.data_dir / "cache" / "audio",
                budget=config.audio_cache_bytes,
                maximum=config.audio_file_max_bytes,
                reserve=config.audio_free_reserve_bytes,
                policy=config.audio_policy,
                saved_directory=config.data_dir / "saved" / "audio",
                pins=library.pins(),
                media_files=media_files,
                media_placement=media_placement,
                metadata_writer=metadata_writer,
                descriptor_factory=descriptor_factory,
            )
            audio = AudioCache(
                storage,
                selected.audio
                if selected.audio is not None
                else AudioProvider(),
                AudioResponse,
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
        radio = Radio(
            RadioRepository(database),
            RadioCatalog(catalog),
            library,
            LastfmSession(lastfm, client),
        )
        resources.push_async_callback(radio.close)
        lyrics = Lyrics(LyricsRepository(database), catalog, Lrclib(client))
        resources.push_async_callback(lyrics.close)
        sync = LastfmSync(
            SyncRepository(database),
            catalog,
            library,
            LastfmSession(lastfm, client),
            credentials,
        )
        resources.push_async_callback(sync.close)
        mixes = Mixes(
            MixRepository(database),
            catalog,
            library,
            LastfmSession(lastfm, client),
            refresh_interval=config.mix_refresh_hours * 3600,
            inactive_ttl=config.mix_inactive_days * 86400,
        )
        resources.push_async_callback(mixes.close)
        imports = Imports(
            ImportRepository(database),
            PlaylistProvider(
                catalog.provider.fetch, catalog.song, catalog.match_loved
            ),
            catalog,
            library,
        )
        resources.push_async_callback(imports.close)
        playlists = Playlists(
            PlaylistRepository(database, library.repository),
            catalog,
            library,
            mixes,
            radio,
            imports,
        )
        if start_workers:
            await sync.start()
            await mixes.start()
            await imports.start()
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
            administration=Administration(
                DashboardRepository(database, config.audio_policy),
                StorageUsage(storage),
                downloads=library.retry,
                mixes=mixes.request,
                sync=sync.control,
                charts=imports.retry_shared,
            ),
        )
