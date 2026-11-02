"""Logical ownership and physical bytes are deliberately separate totals."""

from collections import Counter


def account_storage(users, retained, entries, policy):
    owners = Counter(
        source for sources in retained.values() for source in sources
    )
    saved = {
        key: entry for key, entry in entries.items() if entry.get("saved")
    }
    for user in users:
        sources = retained.get(user["id"], set())
        files = {
            source: saved[f"{source}-{policy}"]
            for source in sources
            if f"{source}-{policy}" in saved
        }
        user.update(
            retained=len(sources),
            downloaded=len(files),
            logical_bytes=sum(e["size"] for e in files.values()),
            exclusive_bytes=sum(
                e["size"] for source, e in files.items() if owners[source] == 1
            ),
        )
    return {
        "saved_bytes": sum(e["size"] for e in saved.values()),
        "cache_bytes": sum(
            e["size"] for e in entries.values() if not e.get("saved")
        ),
        "shared_bytes": sum(
            e["size"]
            for key, e in saved.items()
            if owners[key.rsplit("-", 1)[0]] > 1
        ),
    }
