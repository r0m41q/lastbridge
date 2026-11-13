from uuid import uuid4

from lastbridge.shared.errors import CatalogError


class StationRepository:
    def __init__(self, database):
        self.database = database

    def rows(self):
        with self.database.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM internet_radio_stations "
                    "ORDER BY name COLLATE NOCASE,id"
                )
            ]

    def create(self, name, stream, homepage, artwork):
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO internet_radio_stations "
                "(id,name,stream_url,homepage_url,artwork_url) "
                "VALUES(?,?,?,?,?)",
                ("ir" + uuid4().hex, name, stream, homepage, artwork),
            )

    def update(self, item_id, name, stream, homepage, artwork):
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE internet_radio_stations SET name=?,stream_url=?,"
                "homepage_url=?,artwork_url=COALESCE(?,artwork_url) "
                "WHERE id=?",
                (name, stream, homepage, artwork, item_id),
            ).rowcount
            if not changed:
                raise CatalogError(70, "Radio station not found")

    def remove(self, item_id):
        with self.database.transaction() as connection:
            changed = connection.execute(
                "DELETE FROM internet_radio_stations WHERE id=?", (item_id,)
            ).rowcount
            if not changed:
                raise CatalogError(70, "Radio station not found")
