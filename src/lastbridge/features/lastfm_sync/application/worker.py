import asyncio
import logging
import sqlite3

logger = logging.getLogger(__name__)


async def run(service, operation):
    recover = False
    while True:
        service.wake.clear()
        try:
            if service.remote.configured:
                if recover and operation == service.deliver_one:
                    service.repository.recover_delivery()
                recover = False
                await operation()
        except sqlite3.Error, OSError:
            logger.warning("Last.fm sync storage unavailable; retrying")
            recover = True
        # Idle polling never blocks playback. Imports additionally persist
        # a two-second gap between operations for each account.
        try:
            await asyncio.wait_for(service.wake.wait(), timeout=1)
        except TimeoutError:
            pass
