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
                {"homePageUrl": row["homepage_url"]}
                if row["homepage_url"]
                else {}
            )
            for row in self.repository.rows()
        ]

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
        if item_id is None:
            self.repository.create(name, stream, homepage)
        else:
            self.repository.update(item_id, name, stream, homepage)

    def remove(self, user, item_id):
        self.administrator(user)
        self.repository.remove(item_id)
