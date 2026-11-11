"""Durable progress on the existing import identity and schedule."""

SCHEMA = {
    "state": "TEXT NOT NULL DEFAULT 'complete'",
    "phase": "TEXT NOT NULL DEFAULT 'complete'",
    "progress": "TEXT NOT NULL DEFAULT '{}'",
    "processed": "INTEGER NOT NULL DEFAULT 0",
    "total": "INTEGER NOT NULL DEFAULT 0",
    "matched": "INTEGER NOT NULL DEFAULT 0",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "queued_at": "REAL NOT NULL DEFAULT 0",
}
