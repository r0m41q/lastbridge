import logging

import uvicorn
from pydantic import ValidationError

from lastbridge.app import create_app
from lastbridge.config import Settings
from lastbridge.logging import configure_logging


def main() -> None:
    try:
        settings = Settings()
    except ValidationError:
        configure_logging("ERROR")
        logging.getLogger(__name__).error(
            "Invalid configuration; check LASTBRIDGE_* settings"
        )
        raise SystemExit(2) from None

    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
        log_config=None,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
