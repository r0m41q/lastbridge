from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from lastbridge.features.accounts.adapters.browser.routes import (
    router as accounts_router,
)
from lastbridge.features.administration.adapters.browser.routes import (
    router as administration_router,
)
from lastbridge.features.discovery.adapters.browser.routes import (
    router as discovery_router,
)
from lastbridge.features.lastfm_sync.adapters.browser.routes import (
    router as lastfm_sync_router,
)
from lastbridge.features.library.adapters.browser.routes import (
    router as library_router,
)
from lastbridge.features.playlists.adapters.browser.routes import (
    router as playlists_router,
)
from lastbridge.features.radio.adapters.browser.routes import (
    router as radio_router,
)

router = APIRouter()
router.include_router(administration_router)
router.include_router(accounts_router)
router.include_router(library_router)
router.include_router(lastfm_sync_router)
router.include_router(discovery_router)
router.include_router(playlists_router)
router.include_router(radio_router)


@router.get("/assets/style.css", include_in_schema=False)
async def stylesheet():
    return FileResponse(
        Path(__file__).with_name("static") / "style.css", media_type="text/css"
    )
