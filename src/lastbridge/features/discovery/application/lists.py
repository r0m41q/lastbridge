import json
from uuid import NAMESPACE_URL, uuid5

from lastbridge.features.discovery.application.ports import (
    CatalogAccess,
    LibraryAccess,
    MixStore,
)
from lastbridge.features.discovery.domain.policy import POLICIES, key
from lastbridge.shared.dates import date
from lastbridge.shared.errors import CatalogError


class MixLists:
    def __init__(
        self,
        repository: MixStore,
        catalog: CatalogAccess,
        library: LibraryAccess,
    ):
        self.repository, self.catalog, self.library = (
            repository,
            catalog,
            library,
        )

    def status(self, user_id):
        return self.repository.status(user_id)

    def local(self, user_id, target):
        return self.repository.local(user_id, target)

    def playlists(self, user, item_id=None):
        row = self.status(user["id"])
        snapshot = json.loads(row["snapshot"]) if row else {}
        target = (user["lastfm_name"] or "").casefold()
        _, _, loved, known, removed, removed_ids = self.local(
            user["id"], target
        )
        result = []
        for kind, (name, description) in POLICIES.items():
            playlist_id = (
                "p"
                + uuid5(
                    NAMESPACE_URL, f"lastbridge:mix:{user['id']}:{kind}"
                ).hex
            )
            if item_id is not None and item_id != playlist_id:
                continue
            songs = (
                loved
                if kind == "loved"
                else [
                    self.catalog.read(s, "song")["document"]
                    for s in snapshot.get(kind, [])
                ]
            )
            seen, tracks = set(), []
            profile = json.loads(row["profile"]) if row else {}
            excluded = known | {key(r) for r in profile.get("familiar", [])}
            for song in songs:
                recording = key((song.get("artist", ""), song["title"]))
                if (
                    recording in seen
                    or recording in removed
                    or song["id"] in removed_ids
                ):
                    continue
                if kind == "discover" and recording in excluded:
                    continue
                seen.add(recording)
                tracks.append(song)
            stamp = row["created"] if row else 0
            changed = (
                max(
                    row["created"],
                    row["updated"],
                    user["library_changed"] / 1000,
                )
                if row
                else stamp
            )
            document = {
                "id": playlist_id,
                "name": name,
                "owner": user["username"],
                "public": False,
                "readonly": True,
                "comment": description + " Server-managed; read only.",
                "created": date(stamp),
                "changed": date(changed),
                "songCount": len(tracks),
                "duration": sum(s.get("duration", 0) for s in tracks),
            }
            if tracks and tracks[0].get("coverArt"):
                document["coverArt"] = tracks[0]["coverArt"]
            if item_id is not None:
                document["entry"] = self.library.decorate(user["id"], tracks)
            result.append(document)
        if item_id is not None:
            if not result:
                raise CatalogError(70, "Playlist not found")
            return result[0]
        return result
