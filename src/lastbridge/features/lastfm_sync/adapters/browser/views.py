IMPORT_ACTIONS = (
    ("pause", "Pause import"),
    ("resume", "Resume import"),
    ("refresh", "Refresh Last.fm loves"),
    ("retry-matches", "Retry unmatched recordings"),
)


def sync_view(sync, user):
    job, counts, items, reports = sync.snapshot(user["id"])
    return {
        "job": job,
        "counts": sorted(counts.items()),
        "items": items,
        "reports": reports,
        "loves": sync.loves(user["id"]),
        "import_actions": IMPORT_ACTIONS,
    }
