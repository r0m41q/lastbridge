import logging
from logging.config import dictConfig


def configure_logging(level: str) -> None:
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)s %(name)s: %(message)s"
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                }
            },
            "root": {"handlers": ["console"], "level": level},
            "loggers": {
                "uvicorn": {"handlers": [], "propagate": True, "level": level},
                "uvicorn.error": {
                    "handlers": [],
                    "propagate": True,
                    "level": level,
                },
                # HTTP client logs can contain credential-bearing URLs.
                "httpx": {
                    "handlers": [],
                    "propagate": True,
                    "level": "WARNING",
                },
                "httpcore": {
                    "handlers": [],
                    "propagate": True,
                    "level": "WARNING",
                },
                "uvicorn.access": {
                    "handlers": [],
                    "propagate": False,
                    "level": "CRITICAL",
                },
            },
        }
    )
    logging.getLogger("uvicorn.access").disabled = True
