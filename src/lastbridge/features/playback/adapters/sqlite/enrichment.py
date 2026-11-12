import json
import time


class EnrichmentRepository:
    def __init__(self, database):
        self.database = database

    def get(self, source, policy):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT document,updated FROM media_enrichment "
                "WHERE source=? AND policy=?",
                (source, policy),
            ).fetchone()
        return (
            json.loads(row["document"]) | {"checked": row["updated"]}
            if row
            else {}
        )

    def save(self, source, policy, document):
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO media_enrichment VALUES(?,?,?,?) "
                "ON CONFLICT(source,policy) DO UPDATE SET "
                "document=excluded.document,updated=excluded.updated",
                (source, policy, json.dumps(document), time.time()),
            )
