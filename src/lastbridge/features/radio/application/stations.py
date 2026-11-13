from hashlib import sha256

from lastbridge.features.radio.application.station_ports import StationStore
from lastbridge.features.radio.domain.stations import station_name, station_url
from lastbridge.shared.errors import CatalogError


class Stations:
    def __init__(self, repository: StationStore):
        self.repository = repository

    def list(self):
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "streamUrl": row["stream_url"],
            }
            | (
                {
                    "artworkUrl": row["artwork_url"],
                    "coverArt": self.cover_id(row),
                }
                if row["artwork_url"]
                else {}
            )
            | (
                {"homePageUrl": row["homepage_url"]}
                if row["homepage_url"]
                else {}
            )
            for row in self.repository.rows()
        ]

    @staticmethod
    def cover_id(row):
        revision = sha256(row["artwork_url"].encode()).hexdigest()[:16]
        return f"station-{row['id']}-{revision}"

    def artwork_url(self, cover_id):
        for row in self.repository.rows():
            if row["artwork_url"] and self.cover_id(row) == cover_id:
                return row["artwork_url"]
        raise CatalogError(70, "Radio station artwork not found")

    @staticmethod
    def administrator(user):
        if not user["admin"]:
            raise CatalogError(50, "Administrator access required")

    def save(self, user, values, item_id=None):
        self.administrator(user)
        name = station_name(values.get("name"))
        stream = station_url(values.get("streamUrl"), "streamUrl")
        homepage = station_url(
            values.get("homepageUrl", ""), "homepageUrl", optional=True
        )
        artwork = (
            station_url(values["artworkUrl"], "artworkUrl", optional=True)
            if "artworkUrl" in values
            else None
        )
        if item_id is None:
            self.repository.create(name, stream, homepage, artwork or "")
        else:
            self.repository.update(item_id, name, stream, homepage, artwork)

    def remove(self, user, item_id):
        self.administrator(user)
        self.repository.remove(item_id)
