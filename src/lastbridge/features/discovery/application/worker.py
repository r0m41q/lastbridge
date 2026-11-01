import asyncio
import logging
import sqlite3

logger = logging.getLogger(__name__)


async def run(service):
    while True:
        service.wake.clear()
        try:
            worked = await service.refresh_one()
        except sqlite3.Error, OSError:
            logger.warning("Mix storage unavailable; retrying later")
            # Recover a claim if publication failed, keeping old snapshots.
            try:
                service.repository.recover()
            except sqlite3.Error, OSError:
                pass
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(service.wake.wait(), timeout=1)
            except TimeoutError:
                pass
