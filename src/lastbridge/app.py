import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import fields

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from lastbridge.bootstrap import Providers, bootstrap
from lastbridge.config import Settings
from lastbridge.subsonic import router as subsonic_router
from lastbridge.web import router as web_router

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    start_workers: bool = True,
    providers: Providers | None = None,
) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        logger.info("Starting Lastbridge")
        try:
            async with bootstrap(
                config, start_workers=start_workers, providers=providers
            ) as services:
                app.state.services = services
                # R2/R3 bridge for routes; Services owns the wiring.
                for field in fields(services):
                    setattr(
                        app.state, field.name, getattr(services, field.name)
                    )
                app.state.ready = True
                logger.info("Lastbridge ready")
                try:
                    yield
                finally:
                    app.state.ready = False
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
