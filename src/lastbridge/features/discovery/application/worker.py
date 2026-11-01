import asyncio
import logging

from lastbridge.shared.errors import StorageError

logger = logging.getLogger(__name__)


async def run(service):
    while True:
        service.wake.clear()
        try:
            worked = await service.refresh_one()
        except StorageError, OSError:
            logger.warning("Mix storage unavailable; retrying later")
            # Recover a claim if publication failed, keeping old snapshots.
            try:
                service.repository.recover()
            except StorageError, OSError:
                pass
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(service.wake.wait(), timeout=1)
            except TimeoutError:
                pass
