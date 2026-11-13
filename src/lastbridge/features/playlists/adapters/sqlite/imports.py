"""Atomic snapshots, durable import progress and fenced worker leases."""

import json
import time
from uuid import uuid4

from lastbridge.features.playlists.domain.imports import (
    DAY,
    normalized_entries,
    reviewed_entries,
)
from lastbridge.features.playlists.domain.matching import identity
from lastbridge.shared.errors import CatalogError


class ImportRepository:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def decode(row):
        progress = json.loads(row["progress"])
        if "entries" in progress:
            progress["entries"] = normalized_entries(progress["entries"])
        return dict(row) | {
            "entries": normalized_entries(json.loads(row["entries"])),
            "progress": progress,
            "title": row["custom_title"] or row["title"],
        }

    def visible(self, user):
        with self.database.connection() as connection:
            return [
                self.decode(row)
                for row in connection.execute(
                    "SELECT * FROM playlist_imports WHERE ready=1 AND "
                    "(shared=1 OR owner_id=?) ORDER BY created,id",
                    (user["id"],),
                )
            ]

    def get(self, user, item_id):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM playlist_imports WHERE id=? AND "
                "((ready=1 AND shared=1) OR owner_id=? OR (shared=1 AND ?))",
                (item_id, user["id"], bool(user["admin"])),
            ).fetchone()
        if row is None or (
            not row["ready"]
            and row["state"] == "complete"
            and row["updated"] < time.time() - 3600
        ):
            raise CatalogError(70, "Import not found")
        return self.decode(row)

    def queue(self, user, offset=0, limit=50):
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT p.id,p.owner_id,u.username,p.source,p.shared,"
                "p.ready,COALESCE(p.custom_title,p.title) AS title,"
                "p.state,p.phase,p.processed,p.total,p.matched,p.attempts,"
                "p.error,p.next_attempt,p.queued_at FROM playlist_imports p "
                "JOIN users u ON u.id=p.owner_id WHERE (? OR p.owner_id=?) "
                "AND (p.ready=1 OR p.state!='complete' OR p.updated>=?) "
                "ORDER BY CASE p.state WHEN 'running' THEN 0 "
                "WHEN 'queued' THEN 1 WHEN 'paused' THEN 2 "
                "WHEN 'failed' THEN 3 ELSE 4 END,"
                "CASE WHEN p.state IN ('queued','running') "
                "THEN p.queued_at END,p.queued_at DESC,p.id "
                "LIMIT ? OFFSET ?",
                (
                    bool(user["admin"]),
                    user["id"],
                    time.time() - 3600,
                    limit,
                    offset,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def enqueue(self, user, source, shared):
        now, item_id = time.time(), "y" + uuid4().hex
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM playlist_imports WHERE ready=0 AND "
                "state='complete' AND updated<?",
                (now - 3600,),
            )
            existing = connection.execute(
                "SELECT id FROM playlist_imports WHERE source=? AND "
                "shared=? AND ready=0 AND state!='cancelled' AND "
                "(owner_id=? OR shared=1) ORDER BY created LIMIT 1",
                (source, int(shared), user["id"]),
            ).fetchone()
            if existing:
                return existing["id"]
            count = connection.execute(
                "SELECT count(*) FROM playlist_imports WHERE owner_id=?",
                (user["id"],),
            ).fetchone()[0]
            if count >= 200:
                raise CatalogError(10, "Limit of 200 imports per account")
            connection.execute(
                "INSERT INTO playlist_imports "
                "(id,owner_id,source,shared,title,entries,created,updated,"
                "next_attempt,state,phase,queued_at) "
                "VALUES(?,?,?,?,?,'[]',?,?,0,'queued','fetch',?)",
                (
                    item_id,
                    user["id"],
                    source,
                    int(shared),
                    "YouTube playlist",
                    now,
                    now,
                    now,
                ),
            )
        return item_id

    def confirm(self, user, item_id):
        now = time.time()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM playlist_imports WHERE id=? AND "
                "(owner_id=? OR (shared=1 AND ?)) AND ready=0 "
                "AND state='complete' AND updated>=?",
                (item_id, user["id"], bool(user["admin"]), now - 3600),
            ).fetchone()
            if row is None:
                raise CatalogError(70, "Preview is unfinished or expired")
            if row["shared"] and not user["admin"]:
                raise CatalogError(50, "Administrator access required")
            existing = connection.execute(
                "SELECT id FROM playlist_imports WHERE source=? AND "
                "shared=? AND ready=1 AND (shared=1 OR owner_id=?)",
                (row["source"], row["shared"], user["id"]),
            ).fetchone()
            if existing:
                connection.execute(
                    "DELETE FROM playlist_imports WHERE id=?", (item_id,)
                )
                return existing["id"]
            connection.execute(
                "UPDATE playlist_imports SET ready=1,updated=?,last_checked=?,"
                "next_attempt=? WHERE id=?",
                (now, now, now + DAY * row["refresh_days"], item_id),
            )
        return item_id

    @staticmethod
    def fresh(progress):
        # Keep the last successful source snapshot for unchanged refreshes.
        return {
            "snapshot": progress.get("snapshot"),
            "matches": progress.get("matches", {}),
            "decisions": progress.get("decisions", {}),
            "originals": progress.get("originals", {}),
            "review_undo": progress.get("review_undo"),
        }

    def decide(
        self,
        user,
        item_id,
        source_key,
        song_id,
        acknowledge,
        action="choose",
        undo="",
    ):
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM playlist_imports WHERE id=? AND "
                "(owner_id=? OR (shared=1 AND ?))",
                (item_id, user["id"], bool(user["admin"])),
            ).fetchone()
            if row is None:
                raise CatalogError(70, "Import not found")
            if row["shared"] and not user["admin"]:
                raise CatalogError(50, "Administrator access required")
            if (
                not row["ready"]
                and row["state"] == "complete"
                and row["updated"] < time.time() - 3600
            ):
                raise CatalogError(70, "Preview expired")
            decoded = self.decode(row)
            progress, published = decoded["progress"], decoded["entries"]
            entries = progress.get("entries", []) + published
            entries = [e for e in entries if e.get("source_key") == source_key]
            if not entries:
                raise CatalogError(10, "Entry unavailable")
            decisions = progress.setdefault("decisions", {})
            originals = progress.setdefault("originals", {})
            originals.setdefault(source_key, entries[0])
            previous = decisions.get(source_key)
            if action == "undo":
                saved = progress.get("review_undo") or {}
                if (
                    saved.get("token") != undo
                    or saved.get("key") != source_key
                ):
                    raise CatalogError(10, "Choice changed; reopen this track")
                previous_choice = saved["decision"]
                if previous_choice is None:
                    decisions.pop(source_key, None)
                else:
                    decisions[source_key] = previous_choice
            elif action == "skip":
                included = previous
                if previous and previous.get("skipped"):
                    included = previous.get("included")
                decisions[source_key] = {
                    "song_id": None,
                    "skipped": True,
                    "included": included,
                }
            elif action == "restore" and previous and previous.get("included"):
                decisions[source_key] = previous["included"]
            elif action == "restore" or (action == "choose" and not song_id):
                decisions.pop(source_key, None)
            elif action == "choose":
                candidates = [
                    c
                    for e in entries
                    for c in e.get("candidates", [])
                    if c["song_id"] == song_id
                ]
                if not candidates:
                    raise CatalogError(10, "Candidate unavailable")
                if candidates[0]["mismatch"] and not acknowledge:
                    raise CatalogError(10, "Acknowledge recording mismatch")
                decisions[source_key] = candidates[0]
            else:
                raise CatalogError(10, "Unknown review action")
            ticket = uuid4().hex
            progress["review_undo"] = {
                "token": ticket,
                "key": source_key,
                "decision": previous,
            }

            def apply(rows):
                return reviewed_entries(
                    [
                        (originals[source_key] | {"position": e["position"]})
                        if e.get("source_key") == source_key
                        else e
                        for e in rows
                    ],
                    decisions,
                )

            published = apply(published)
            if "entries" in progress:
                progress["entries"] = apply(progress["entries"])
            counted = progress.get("entries")
            if counted is None and row["phase"] == "complete":
                counted = published
            matched = (
                sum(bool(e.get("song_id")) for e in counted)
                if counted is not None
                else row["matched"]
            )
            connection.execute(
                "UPDATE playlist_imports SET entries=?,progress=?,"
                "matched=?,updated=? WHERE id=?",
                (
                    json.dumps(published),
                    json.dumps(progress),
                    matched,
                    time.time()
                    if published != json.loads(row["entries"])
                    else row["updated"],
                    item_id,
                ),
            )
            return ticket

    def request(self, item_id):
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM playlist_imports WHERE id=? AND ready=1",
                (item_id,),
            ).fetchone()
            if row is None:
                raise CatalogError(70, "Import not found")
            if row["state"] in ("queued", "running"):
                return
            progress = self.fresh(json.loads(row["progress"]))
            # Explicit refresh retries matching for an unchanged source too.
            progress.pop("snapshot", None)
            connection.execute(
                "UPDATE playlist_imports SET state='queued',phase='fetch',"
                "progress=?,processed=0,total=0,matched=0,attempts=0,"
                "token=NULL,error=NULL,next_attempt=0,queued_at=? WHERE id=?",
                (json.dumps(progress), time.time(), item_id),
            )

    def claim(self, item_id=None):
        now, token = time.time(), uuid4().hex
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM playlist_imports WHERE "
                "((state IN ('queued','running') AND next_attempt<=?) OR "
                "(state='complete' AND ready=1 AND "
                "((shared=1 AND next_attempt<=?) OR id=?))) "
                "AND (? IS NULL OR id=?) ORDER BY "
                "CASE state WHEN 'queued' THEN 0 ELSE 1 END,"
                "queued_at,created,id LIMIT 1",
                (now, now, item_id, item_id, item_id),
            ).fetchone()
            if row is None:
                return None
            job = self.decode(row)
            if row["state"] == "complete":
                job.update(
                    progress=self.fresh(job["progress"]),
                    phase="fetch",
                    processed=0,
                    total=0,
                    matched=0,
                    attempts=0,
                    queued_at=now,
                )
            connection.execute(
                "UPDATE playlist_imports SET state='running',token=?,"
                "next_attempt=?,progress=?,phase=?,processed=?,total=?,"
                "matched=?,attempts=?,queued_at=? WHERE id=?",
                (
                    token,
                    now + 600,
                    json.dumps(job["progress"]),
                    job["phase"],
                    job["processed"],
                    job["total"],
                    job["matched"],
                    job["attempts"],
                    job["queued_at"],
                    row["id"],
                ),
            )
        return job | {"token": token, "state": "running"}

    def reviewed_progress(self, connection, job, progress):
        # A user can select a recording while an older batch is in flight.
        # Merge the current decisions inside the same fenced transaction.
        row = connection.execute(
            "SELECT progress FROM playlist_imports WHERE id=? AND token=? "
            "AND state='running'",
            (job["id"], job["token"]),
        ).fetchone()
        if row is None:
            return None
        keys = {
            identity(track)
            for track in progress.get("tracks", [])
            if isinstance(track, dict)
        } | {entry.get("source_key") for entry in progress["entries"]}
        current = json.loads(row["progress"])
        saved = current.get("decisions", {})
        decisions = {
            key: decision
            for key, decision in saved.items()
            if key in keys or decision.get("skipped")
        }
        originals = {
            key: entry
            for key, entry in current.get("originals", {}).items()
            if key in keys or key in decisions
        }
        for entry in progress["entries"]:
            key = entry.get("source_key")
            if (
                key
                and not entry.get("reviewed")
                and (key in decisions or key in originals)
            ):
                originals[key] = entry
        entries = [
            originals.get(e.get("source_key"), e) | {"position": e["position"]}
            for e in progress["entries"]
        ]
        return progress | {
            "decisions": decisions,
            "originals": originals,
            "review_undo": current.get("review_undo"),
            "entries": reviewed_entries(entries, decisions),
        }

    def checkpoint(self, job, progress):
        with self.database.transaction() as connection:
            progress = self.reviewed_progress(connection, job, progress)
            if progress is None:
                return False
            entries = progress["entries"]
            matched = sum(bool(entry.get("song_id")) for entry in entries)
            changed = connection.execute(
                "UPDATE playlist_imports SET progress=?,phase='resolve',"
                "processed=?,total=?,matched=?,attempts=0,error=NULL,"
                "title=CASE WHEN ready=0 THEN ? ELSE title END,"
                "next_attempt=? WHERE id=? AND token=? AND state='running'",
                (
                    json.dumps(progress),
                    len(entries),
                    len(progress["tracks"]),
                    matched,
                    progress["title"],
                    time.time() + 600,
                    job["id"],
                    job["token"],
                ),
            ).rowcount
        return bool(changed)

    def publish(self, job, snapshot):
        now = time.time()
        with self.database.transaction() as connection:
            progress = self.reviewed_progress(
                connection,
                job,
                job["progress"] | {"entries": snapshot.entries},
            )
            if progress is None:
                return False
            entries = progress["entries"]
            current = connection.execute(
                "SELECT * FROM playlist_imports WHERE id=?", (job["id"],)
            ).fetchone()
            previous_title = current["custom_title"] or current["title"]
            next_title = current["custom_title"] or snapshot.title
            content_changed = (
                not current["ready"]
                or previous_title != next_title
                or normalized_entries(json.loads(current["entries"]))
                != entries
            )
            if "tracks" in progress:
                progress["snapshot"] = {
                    "tracks": progress["tracks"],
                    "entries": [
                        progress.get("originals", {}).get(
                            entry.get("source_key"), entry
                        )
                        | {"position": entry["position"]}
                        for entry in entries
                    ],
                    "resolver_version": progress.get("resolver_version"),
                }
            changed = connection.execute(
                "UPDATE playlist_imports SET title=?,entries=?,updated=?,"
                "last_checked=?,next_attempt=?,token=NULL,error=NULL,"
                "state='complete',"
                "phase='complete',attempts=0,processed=?,total=?,matched=?,"
                "progress=? "
                "WHERE id=? AND token=? AND state='running'",
                (
                    snapshot.title,
                    json.dumps(entries),
                    now if content_changed else current["updated"],
                    now,
                    now + DAY * current["refresh_days"],
                    len(entries),
                    len(entries),
                    sum(bool(e.get("song_id")) for e in entries),
                    json.dumps(self.fresh(progress)),
                    job["id"],
                    job["token"],
                ),
            ).rowcount
        return bool(changed)

    def failed(self, job, error="Import failed", retry=True):
        # Attempts are consecutive failures of a fetch/batch, not tracks.
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT attempts FROM playlist_imports WHERE id=? "
                "AND token=? AND state='running'",
                (job["id"], job["token"]),
            ).fetchone()
            if row is None:
                return
            attempts = row["attempts"] + 1
            state = "queued" if retry and attempts < 3 else "failed"
            connection.execute(
                "UPDATE playlist_imports SET state=?,attempts=?,token=NULL,"
                "error=?,next_attempt=? WHERE id=? AND token=?",
                (
                    state,
                    attempts,
                    error,
                    time.time() + 60 * attempts,
                    job["id"],
                    job["token"],
                ),
            )

    def release(self, job):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE playlist_imports SET state='queued',token=NULL,"
                "next_attempt=0 WHERE id=? AND token=? AND state='running'",
                (job["id"], job["token"]),
            )

    def control(self, user, item_id, action):
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM playlist_imports WHERE id=? AND "
                "(owner_id=? OR ?)",
                (item_id, user["id"], bool(user["admin"])),
            ).fetchone()
            if row is None:
                raise CatalogError(70, "Import not found")
            if row["shared"] and not user["admin"]:
                raise CatalogError(50, "Administrator access required")
            state = row["state"]
            allowed = {
                "pause": {"queued", "running"},
                "resume": {"paused"},
                "cancel": {"queued", "running", "paused", "failed"},
                "retry": {"failed", "cancelled"},
            }
            if action not in allowed or state not in allowed[action]:
                raise CatalogError(10, "Action unavailable for this job")
            target = {
                "pause": "paused",
                "resume": "queued",
                "cancel": "cancelled",
                "retry": "queued",
            }[action]
            connection.execute(
                "UPDATE playlist_imports SET state=?,token=NULL,"
                "attempts=0,error=NULL,next_attempt=0 WHERE id=?",
                (target, item_id),
            )

    def remove(self, item_id):
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM playlist_imports WHERE id=?", (item_id,)
            )

    def retry_shared(self, item_id):
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM playlist_imports WHERE id=? AND shared=1 "
                "AND ready=1 AND token IS NULL AND error IS NOT NULL",
                (item_id,),
            ).fetchone()
            if row is None:
                return
            progress = json.loads(row["progress"])
            phase = row["phase"]
            if phase == "complete":
                progress, phase = self.fresh(progress), "fetch"
            connection.execute(
                "UPDATE playlist_imports SET state='queued',next_attempt=0,"
                "attempts=0,error=NULL,progress=?,phase=? WHERE id=?",
                (json.dumps(progress), phase, item_id),
            )

    def rename_shared(self, item_id, name):
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE playlist_imports SET custom_title=?,updated=? "
                "WHERE id=? AND shared=1 AND ready=1",
                (name, time.time(), item_id),
            ).rowcount
            if not changed:
                raise CatalogError(70, "Shared chart not found")

    def schedule_shared(self, item_id, days):
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE playlist_imports SET refresh_days=?,next_attempt="
                "CASE WHEN state='complete' THEN last_checked+? "
                "ELSE next_attempt END WHERE id=? AND shared=1 AND ready=1",
                (days, days * DAY, item_id),
            ).rowcount
            if not changed:
                raise CatalogError(70, "Shared chart not found")

    def recover(self):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE playlist_imports SET state='queued',token=NULL,"
                "next_attempt=0 WHERE state='running'"
            )
            connection.execute(
                "DELETE FROM playlist_imports WHERE ready=0 AND "
                "state='complete' AND updated<?",
                (time.time() - 3600,),
            )
