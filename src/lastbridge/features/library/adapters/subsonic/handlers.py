from lastbridge.shared.errors import CatalogError
from lastbridge.transport.http.context import services
from lastbridge.transport.subsonic.parameters import required
from lastbridge.transport.subsonic.serialization import directory_album


async def favorites(request, method, values, user):
    library = services(request).library
    user_id = user["id"]
    if request.method == "HEAD":
        raise CatalogError(10, "Use GET or POST to change favorites")
    targets = []
    for name, kind in (
        ("id", None),
        ("albumId", "album"),
        ("artistId", "artist"),
    ):
        for item_id in values.get(name, []):
            if not item_id:
                raise CatalogError(10, f"Invalid parameter: {name}")
            targets.append((item_id, kind))
    await library.change(user_id, targets, method == "star")
    return {}


async def folders(request, method, values, user):
    return {
        "musicFolders": {"musicFolder": [{"id": 1, "name": "Saved library"}]}
    }


async def scan_status(request, method, values, user):
    library = services(request).library
    user_id = user["id"]
    return {
        "scanStatus": {
            "scanning": False,
            "count": len(library.collection(user_id)["song"]),
        }
    }


async def artists(request, method, values, user):
    library = services(request).library
    user_id = user["id"]
    artists = library.collection(user_id)["artist"]
    groups = {}
    for artist in artists:
        letter = artist["name"][:1].upper()
        letter = letter if letter.isalpha() else "#"
        groups.setdefault(letter, []).append(artist)
    return {
        "artists" if method == "getArtists" else "indexes": {
            "ignoredArticles": "",
            "index": [
                {"name": key, "artist": group}
                for key, group in sorted(groups.items())
            ],
            **(
                {"lastModified": user["library_changed"]}
                if method == "getIndexes"
                else {}
            ),
        }
    }


async def genres(request, method, values, user):
    return {"genres": {"genre": []}}


async def directory(request, method, values, user):
    catalog = services(request).catalog
    library = services(request).library
    user_id = user["id"]
    item_id = required(values, "id")
    if item_id == "1":
        saved = library.collection(user_id)
        return {
            "directory": {
                "id": "1",
                "name": "Saved library",
                "child": [
                    dict(a, isDir=True, parent="1") for a in saved["artist"]
                ]
                + [s for s in saved["song"] if not s.get("artistId")],
            }
        }
    stored = catalog.read(item_id)
    if stored["kind"] == "song":
        raise CatalogError(70, "Directory not found")
    item = await catalog.detail(item_id, stored["kind"])
    children = (
        item.get("song", [])
        if stored["kind"] == "album"
        else [directory_album(a) for a in item.get("album", [])]
    )
    return {
        "directory": {
            "id": item_id,
            "name": item["name"],
            "parent": item.get("artistId", "1"),
            "child": library.decorate(user_id, children),
        }
    }


HANDLERS = {
    "star": favorites,
    "unstar": favorites,
    "getMusicFolders": folders,
    "getScanStatus": scan_status,
    "getArtists": artists,
    "getIndexes": artists,
    "getGenres": genres,
    "getMusicDirectory": directory,
}
