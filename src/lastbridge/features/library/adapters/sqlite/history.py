"""Listening statistics derived from accepted local playback submissions."""

SONG_PLAYS = (
    "WITH song_plays AS ("
    "SELECT song_id,COUNT(DISTINCT timestamp) AS count,"
    "MAX(timestamp) AS last FROM scrobbles WHERE user_id=? GROUP BY song_id) "
)
ALBUM_PLAYS = (
    "SELECT album.id,SUM(p.count) AS count,MAX(p.last) AS last "
    "FROM song_plays p JOIN catalog song ON song.id=p.song_id "
    "AND song.kind='song' JOIN catalog album "
    "ON album.id=json_extract(song.document,'$.albumId') "
    "AND album.kind='album' GROUP BY album.id "
)


class HistoryRepository:
    def plays(self, user_id):
        with self.database.connection() as connection:
            rows = connection.execute(
                SONG_PLAYS
                + "SELECT song_id AS id,count,last FROM song_plays UNION ALL "
                + ALBUM_PLAYS,
                (user_id,),
            ).fetchall()
        return {row["id"]: (row["count"], row["last"]) for row in rows}

    def album_history(self, user_id, kind, size, offset):
        # These are fixed SQL fragments, never client-provided sort columns.
        order = {
            "recent": "last DESC,album.id",
            "frequent": "count DESC,last DESC,album.id",
        }[kind]
        with self.database.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    SONG_PLAYS
                    + ALBUM_PLAYS
                    + "ORDER BY "
                    + order
                    + " LIMIT ? OFFSET ?",
                    (user_id, size, offset),
                )
            ]
