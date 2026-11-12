"""A short-lived, fenced undo for the user's latest recording review."""

import json
import time
from uuid import uuid4

from lastbridge.shared.errors import CatalogError


class ReviewUndoRepository:
    @staticmethod
    def review_state(connection, user_id, song_id, key):
        def rows(query, params):
            return [dict(row) for row in connection.execute(query, params)]

        return {
            "account": rows(
                "SELECT library_changed,lastfm_name,link_status FROM users "
                "WHERE id=?",
                (user_id,),
            ),
            "favorites": rows(
                "SELECT * FROM favorites WHERE user_id=? AND item_id=?",
                (user_id, song_id),
            ),
            "tracks": rows(
                "SELECT * FROM favorite_tracks WHERE user_id=? "
                "AND favorite_id=? ORDER BY song_id",
                (user_id, song_id),
            ),
            "corrections": rows(
                "SELECT * FROM recording_corrections WHERE user_id=? "
                "AND song_id=?",
                (user_id, song_id),
            ),
            "loves": rows(
                "SELECT * FROM loved_items WHERE user_id=? "
                "AND (song_id=? OR recording_key=?) "
                "ORDER BY target,recording_key",
                (user_id, song_id, key),
            ),
            "removals": rows(
                "SELECT * FROM love_removals WHERE user_id=? "
                "ORDER BY recording_key",
                (user_id,),
            ),
            "favorite_removal": rows(
                "SELECT * FROM favorite_removals WHERE user_id=? "
                "AND item_id=?",
                (user_id, song_id),
            ),
        }

    def save_review_undo(
        self, connection, user_id, kind, key, song_id, before
    ):
        ticket = uuid4().hex
        after = self.review_state(connection, user_id, song_id, key)
        connection.execute(
            "INSERT INTO recording_review_undo VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET token=excluded.token,"
            "document=excluded.document,expires=excluded.expires",
            (
                user_id,
                ticket,
                json.dumps(
                    {
                        "kind": kind,
                        "key": key,
                        "song_id": song_id,
                        "before": before,
                        "after": after,
                    }
                ),
                time.time() + 900,
            ),
        )
        return ticket

    def review_undo_token(self, user_id):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT token FROM recording_review_undo "
                "WHERE user_id=? AND expires>?",
                (user_id, time.time()),
            ).fetchone()
        return row[0] if row else None

    def undo_review(self, user_id, kind, key, token):
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT document FROM recording_review_undo WHERE user_id=? "
                "AND token=? AND expires>?",
                (user_id, token, time.time()),
            ).fetchone()
            if row is None:
                raise CatalogError(
                    10, "Undo expired or another choice was made"
                )
            saved = json.loads(row[0])
            song_id = saved["song_id"]
            if (
                saved["kind"] != kind
                or saved["key"] != key
                or (
                    self.review_state(connection, user_id, song_id, key)
                    != saved["after"]
                )
            ):
                raise CatalogError(10, "Library changed; reopen this track")
            before = saved["before"]
            for table, field in (
                ("favorites", "item_id"),
                ("recording_corrections", "song_id"),
                ("favorite_removals", "item_id"),
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE user_id=? AND {field}=?",
                    (user_id, song_id),
                )
            for table, name in (
                ("favorites", "favorites"),
                ("favorite_tracks", "tracks"),
                ("recording_corrections", "corrections"),
                ("favorite_removals", "favorite_removal"),
            ):
                for value in before[name]:
                    columns = ",".join(value)
                    marks = ",".join("?" for _ in value)
                    connection.execute(
                        f"INSERT INTO {table}({columns}) VALUES({marks})",
                        tuple(value.values()),
                    )
            for love in before["loves"]:
                connection.execute(
                    "UPDATE loved_items SET song_id=?,manual_song_id=?,"
                    "state=?,error=?,review_skipped=? WHERE user_id=? "
                    "AND target=? AND recording_key=?",
                    (
                        love["song_id"],
                        love["manual_song_id"],
                        love["state"],
                        love["error"],
                        love["review_skipped"],
                        user_id,
                        love["target"],
                        love["recording_key"],
                    ),
                )
            connection.execute(
                "DELETE FROM recording_reviews WHERE user_id=?",
                (user_id,),
            )
            connection.execute(
                "DELETE FROM recording_review_undo WHERE user_id=?",
                (user_id,),
            )
            self.schedule(connection)
            connection.execute(
                "UPDATE users SET library_changed=MAX(library_changed+1,?) "
                "WHERE id=?",
                (int(time.time() * 1000), user_id),
            )
