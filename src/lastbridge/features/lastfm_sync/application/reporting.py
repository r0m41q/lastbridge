import time

from lastbridge.features.lastfm_sync.domain.reporting import timestamp
from lastbridge.shared.errors import CatalogError


def prepare_report(catalog, values):
    submission = values.get("submission", "true")
    if submission not in ("true", "false"):
        raise CatalogError(10, "Invalid parameter: submission")
    ids, times = values.get("id", []), values.get("time", [])
    if not ids or any(not item for item in ids):
        raise CatalogError(10, "Supply at least one song id")
    if times and len(times) != len(ids):
        raise CatalogError(10, "Supply one time for each song id")
    now = int(time.time())
    prepared = []
    for index, item_id in enumerate(ids):
        song = catalog.read(item_id, "song")["document"]
        stamp = timestamp(times[index], now) if times else now
        prepared.append((song, stamp))
    return prepared, now
