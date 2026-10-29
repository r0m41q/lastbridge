import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from tempfile import TemporaryFile

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from lastbridge.accounts import Accounts
from lastbridge.audio import AudioCache
from lastbridge.catalog import Catalog
from lastbridge.config import Settings
from lastbridge.lastfm import Lastfm
from lastbridge.library import Library
from lastbridge.lyrics import Lyrics
from lastbridge.mixes import Mixes
from lastbridge.playlists import Playlists
from lastbridge.radio import Radio
from lastbridge.subsonic import router as subsonic_router
from lastbridge.sync import LastfmSync
from lastbridge.web import router as web_router

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        logger.info("Starting Lastbridge")
        try:
            for directory in (
                config.data_dir,
                config.data_dir / "cache",
                config.data_dir / "saved",
            ):
                directory.mkdir(parents=True, exist_ok=True)
                # Fail startup rather than report ready with unusable storage.
                with TemporaryFile(dir=directory):
                    pass
        except OSError:
            # Do not put configured paths or environment values in logs.
            raise RuntimeError(
                "Cannot initialize writable Lastbridge storage"
            ) from None

        try:
            app.state.accounts = await run_in_threadpool(
                Accounts, config.data_dir
            )
            app.state.catalog = Catalog(app.state.accounts)
            app.state.library = Library(
                app.state.accounts, app.state.catalog, config.audio_policy
            )
            app.state.radio = Radio(
                app.state.accounts, app.state.catalog, app.state.library
            )
            app.state.lastfm = Lastfm(
                config.lastfm_api_key.get_secret_value(),
                config.lastfm_api_secret.get_secret_value(),
            )
            app.state.password_limit = asyncio.Semaphore(2)
        except Exception:
            raise RuntimeError(
                "Cannot initialize account storage/credentials"
            ) from None

        try:
            app.state.audio = AudioCache(
                config.data_dir / "cache" / "audio",
                budget=config.audio_cache_bytes,
                maximum=config.audio_file_max_bytes,
                reserve=config.audio_free_reserve_bytes,
                policy=config.audio_policy,
                saved_directory=config.data_dir / "saved" / "audio",
                pins=app.state.library.pins(),
            )
        except Exception:
            raise RuntimeError("Cannot initialize audio cache") from None

        try:
            await app.state.library.start(app.state.audio)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                limits=httpx.Limits(
                    max_connections=20, max_keepalive_connections=10
                ),
                follow_redirects=False,
            ) as client:
                app.state.http_client = client
                app.state.radio.lastfm = app.state.lastfm
                app.state.radio.client = client
                app.state.lyrics = Lyrics(
                    app.state.accounts, app.state.catalog, client
                )
                app.state.sync = LastfmSync(
                    app.state.accounts,
                    app.state.catalog,
                    app.state.library,
                    app.state.lastfm,
                    client,
                )
                app.state.mixes = Mixes(
                    app.state.accounts,
                    app.state.catalog,
                    app.state.library,
                    app.state.lastfm,
                    client,
                    refresh_interval=config.mix_refresh_hours * 3600,
                    inactive_ttl=config.mix_inactive_days * 86400,
                )
                app.state.playlists = Playlists(
                    app.state.accounts,
                    app.state.catalog,
                    app.state.library,
                    app.state.mixes,
                    app.state.radio,
                )
                try:
                    await app.state.sync.start()
                    await app.state.mixes.start()
                    app.state.ready = True
                    logger.info("Lastbridge ready")
                    yield
                finally:
                    app.state.ready = False
                    await app.state.lyrics.close()
                    await app.state.radio.close()
                    await app.state.mixes.close()
                    await app.state.sync.close()
        finally:
            try:
                await app.state.library.close()
            finally:
                await app.state.audio.close()
        logger.info("Lastbridge stopped")

    app = FastAPI(
        title="Lastbridge",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.ready = False
    app.state.config = config

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        # no-referrer makes browser navigation POSTs send Origin: null,
        # conflicting with the strict origin check on browser forms.
        # Keep same-origin submissions working without external referrer leaks.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; "
            "form-action 'self' https://www.last.fm; "
            "frame-ancestors 'none'; "
            "base-uri 'none'"
        )
        return response

    app.include_router(web_router)
    app.include_router(subsonic_router)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        if not app.state.ready:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    return app
