import json
import time
from uuid import uuid4

from lastbridge.features.library.adapters.sqlite.review_undo import (
    ReviewUndoRepository,
)
from lastbridge.features.library.domain.recordings import metadata
from lastbridge.shared.errors import CatalogError


class CorrectionRepository(ReviewUndoRepository):
    def recording_ids(self, user_id):
        with self.database.connection() as connection:
            return dict(
                connection.execute(
                    "SELECT song_id,recording_id FROM recording_corrections "
                    "WHERE user_id=?",
                    (user_id,),
                )
            )

    def recording_id(self, user_id, song_id):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT recording_id FROM recording_corrections "
                "WHERE user_id=? AND song_id=?",
                (user_id, song_id),
            ).fetchone()
        return row[0] if row else song_id

    def correction_context(self, user_id, kind, key):
        with self.database.connection() as connection:
            return self._correction_context(connection, user_id, kind, key)

    def _correction_context(self, connection, user_id, kind, key):
        if kind == "song":
            row = connection.execute(
                "SELECT song_id FROM retained_tracks "
                "WHERE user_id=? AND song_id=? UNION "
                "SELECT e.song_id FROM playlists p JOIN playlist_entries e "
                "ON e.playlist_id=p.id WHERE p.user_id=? AND e.song_id=?",
                (user_id, key, user_id, key),
            ).fetchone()
            if row:
                return {"kind": kind, "key": key, "song_id": key}
        elif kind == "love":
            row = connection.execute(
                "SELECT l.* FROM loved_items l JOIN love_imports i "
                "ON i.user_id=l.user_id AND i.target=l.target "
                "JOIN users u ON u.id=l.user_id WHERE l.user_id=? "
                "AND l.recording_key=? AND u.link_status='linked' "
                "AND lower(u.lastfm_name)=l.target "
                "AND (l.state!='skipped' OR l.review_skipped=1) "
                "AND NOT EXISTS (SELECT 1 FROM love_removals r "
                "WHERE r.user_id=l.user_id "
                "AND r.recording_key=l.recording_key)",
                (user_id, key),
            ).fetchone()
            if row:
                return dict(row) | {"kind": kind, "key": key}
        raise CatalogError(70, "Saved recording not found")

    def recording_review(self, user_id, kind, key):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM recording_reviews WHERE user_id=? "
                "AND kind=? AND subject=? AND expires>?",
                (user_id, kind, key, time.time()),
            ).fetchone()
        return (
            dict(row) | {"candidates": json.loads(row["candidates"])}
            if row
            else None
        )

    def save_review(self, user_id, kind, key, fingerprint, candidates):
        with self.database.transaction() as connection:
            self._correction_context(connection, user_id, kind, key)
            connection.execute(
                "DELETE FROM recording_reviews WHERE expires<=?",
                (time.time(),),
            )
            connection.execute(
                "INSERT INTO recording_reviews VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(user_id,kind,subject) DO UPDATE SET "
                "token=excluded.token,fingerprint=excluded.fingerprint,"
                "candidates=excluded.candidates,expires=excluded.expires",
                (
                    user_id,
                    kind,
                    key,
                    uuid4().hex,
                    fingerprint,
                    json.dumps(candidates),
                    time.time() + 900,
                ),
            )
            connection.execute(
                "DELETE FROM recording_reviews WHERE user_id=? AND rowid "
                "NOT IN (SELECT rowid FROM recording_reviews WHERE user_id=? "
                "ORDER BY expires DESC LIMIT 100)",
                (user_id, user_id),
            )

    def choose_recording(
        self,
        user_id,
        kind,
        key,
        token,
        fingerprint,
        recording_id,
        acknowledge=False,
    ):
        with self.database.transaction() as connection:
            context = self._correction_context(connection, user_id, kind, key)
            if context.get("review_skipped"):
                raise CatalogError(10, "Restore this track before choosing")
            if recording_id:
                review = connection.execute(
                    "SELECT * FROM recording_reviews WHERE user_id=? "
                    "AND kind=? AND subject=? AND token=? AND fingerprint=? "
                    "AND expires>?",
                    (user_id, kind, key, token, fingerprint, time.time()),
                ).fetchone()
                candidate = (
                    next(
                        (
                            c
                            for c in json.loads(review["candidates"])
                            if c["song_id"] == recording_id
                        ),
                        None,
                    )
                    if review
                    else None
                )
                if candidate is None:
                    raise CatalogError(
                        10, "Search again before choosing a recording"
                    )
                if candidate["mismatch"] and not acknowledge:
                    raise CatalogError(
                        10, "Confirm the recording differences first"
                    )
                selected = connection.execute(
                    "SELECT document FROM catalog WHERE id=? AND kind='song'",
                    (recording_id,),
                ).fetchone()
                if not selected:
                    raise CatalogError(70, "Recording not found")
                if (
                    metadata(json.loads(selected["document"]))
                    != candidate["metadata"]
                ):
                    raise CatalogError(10, "Recording changed; search again")
            elif kind != "song":
                raise CatalogError(10, "Choose a recording first")
            song_id = context["song_id"]
            before = self.review_state(
                connection, user_id, song_id or recording_id, key
            )
            if song_id is None:
                song_id = recording_id
                connection.execute(
                    "DELETE FROM favorite_removals "
                    "WHERE user_id=? AND item_id=?",
                    (user_id, song_id),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO favorites VALUES(?,?,?)",
                    (
                        user_id,
                        song_id,
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    ),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO favorite_tracks VALUES(?,?,?)",
                    (user_id, song_id, song_id),
                )
            if recording_id and recording_id != song_id:
                connection.execute(
                    "INSERT INTO recording_corrections VALUES(?,?,?) "
                    "ON CONFLICT(user_id,song_id) DO UPDATE SET "
                    "recording_id=excluded.recording_id",
                    (user_id, song_id, recording_id),
                )
            else:
                connection.execute(
                    "DELETE FROM recording_corrections "
                    "WHERE user_id=? AND song_id=?",
                    (user_id, song_id),
                )
            connection.execute(
                "UPDATE loved_items SET manual_song_id=? "
                "WHERE user_id=? AND song_id=?",
                (recording_id or None, user_id, song_id),
            )
            if recording_id:
                connection.execute(
                    "UPDATE loved_items SET state='matched',error=NULL "
                    "WHERE user_id=? AND song_id=? AND state!='skipped'",
                    (user_id, song_id),
                )
            if kind == "love":
                connection.execute(
                    "UPDATE loved_items SET song_id=?,manual_song_id=?,"
                    "state='matched',error=NULL WHERE user_id=? AND target=? "
                    "AND recording_key=?",
                    (song_id, recording_id, user_id, context["target"], key),
                )
            # Any other open report for this user may refer to the old choice.
            connection.execute(
                "DELETE FROM recording_reviews WHERE user_id=?", (user_id,)
            )
            self.schedule(connection)
            connection.execute(
                "UPDATE users SET library_changed=MAX(library_changed+1,?) "
                "WHERE id=?",
                (int(time.time() * 1000), user_id),
            )
            self.save_review_undo(
                connection, user_id, kind, key, song_id, before
            )
        return song_id

    def love_review_queue(self, user_id):
        with self.database.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT l.recording_key,l.artist,l.title,l.song_id,"
                    "l.state,"
                    "l.review_skipped,l.error FROM loved_items l "
                    "JOIN love_imports i ON i.user_id=l.user_id "
                    "AND i.target=l.target JOIN users u ON u.id=l.user_id "
                    "WHERE l.user_id=? AND u.link_status='linked' "
                    "AND lower(u.lastfm_name)=l.target "
                    "AND (l.state='unmatched' OR l.review_skipped=1 "
                    "OR (l.state='matched' AND l.manual_song_id IS NOT NULL)) "
                    "AND NOT EXISTS (SELECT 1 FROM love_removals r "
                    "WHERE r.user_id=l.user_id "
                    "AND r.recording_key=l.recording_key) "
                    "ORDER BY l.artist,l.title,l.recording_key",
                    (user_id,),
                )
            ]

    def skip_love_review(self, user_id, key, skipped):
        with self.database.transaction() as connection:
            context = self._correction_context(
                connection, user_id, "love", key
            )
            if context["song_id"] is not None:
                raise CatalogError(10, "This track is already a favorite")
            before = self.review_state(connection, user_id, None, key)
            connection.execute(
                "UPDATE loved_items SET review_skipped=?,state=?,error=NULL "
                "WHERE user_id=? AND target=? AND recording_key=?",
                (
                    int(skipped),
                    "skipped" if skipped else "unmatched",
                    user_id,
                    context["target"],
                    key,
                ),
            )
            connection.execute(
                "DELETE FROM recording_reviews WHERE user_id=? "
                "AND kind='love' AND subject=?",
                (user_id, key),
            )
            return self.save_review_undo(
                connection, user_id, "love", key, None, before
            )
