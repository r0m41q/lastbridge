import json
import time
from datetime import UTC, datetime

from lastbridge.features.catalog.domain.documents import (
    SOURCE,
    identity,
)
from lastbridge.shared.errors import CatalogError


class CatalogRepository:
    def __init__(self, database, document_policy):
        self.database = database
        self.document_policy = document_policy

    def artists_named(self, name):
        with self.database.connection() as connection:
            return [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM catalog WHERE kind='artist'"
                )
                if json.loads(r["document"])["name"].strip().casefold() == name
            ]

    def read(self, item_id, kind=None):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM catalog WHERE id=?", (item_id,)
            ).fetchone()
        if row is None or (kind is not None and row["kind"] != kind):
            raise CatalogError(70, "Catalog item not found")
        return dict(row) | {
            "document": self.document_policy(
                row["kind"], json.loads(row["document"])
            )
        }

    def put(self, kind, source, document, image=None, complete=False):
        if not isinstance(source, str) or not SOURCE.fullmatch(source):
            raise CatalogError(70, "Catalog item unavailable")
        item_id = identity(kind, source)
        document = {k: v for k, v in document.items() if v is not None}
        document["id"] = item_id
        if image:
            document["coverArt"] = item_id
        with self.database.transaction() as connection:
            old = connection.execute(
                "SELECT document,image,complete FROM catalog WHERE id=?",
                (item_id,),
            ).fetchone()
            if old:
                # Sparse search rows must not erase hydrated album/song data.
                prior = json.loads(old["document"])
                document = (
                    document | prior
                    if old["complete"] and not complete
                    else prior | document
                )
                image = image or old["image"]
                complete = complete or bool(old["complete"])
            if kind == "album":
                document["created"] = (
                    json.loads(old["document"]).get("created") if old else None
                ) or datetime.now(UTC).isoformat().replace("+00:00", "Z")
            document = self.document_policy(kind, document)
            connection.execute(
                "INSERT INTO catalog VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET document=excluded.document,"
                "image=excluded.image,complete=excluded.complete,"
                "updated=excluded.updated",
                (
                    item_id,
                    kind,
                    source,
                    json.dumps(document),
                    image,
                    int(complete),
                    int(time.time()),
                ),
            )
        return document
