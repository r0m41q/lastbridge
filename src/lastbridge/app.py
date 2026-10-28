import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from tempfile import TemporaryFile

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from lastbridge.config import Settings

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

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(
                max_connections=20, max_keepalive_connections=10
            ),
            follow_redirects=False,
        ) as client:
            app.state.http_client = client
            app.state.ready = True
            logger.info("Lastbridge ready")
            try:
                yield
            finally:
                app.state.ready = False
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

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        if not app.state.ready:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    return app
