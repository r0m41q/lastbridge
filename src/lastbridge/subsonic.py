from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from lastbridge.catalog import CatalogError
from lastbridge.web import parameters

router = APIRouter()
EXTENSIONS = [
    {"name": "apiKeyAuthentication", "versions": [1]},
    {"name": "formPost", "versions": [1]},
]


def scalar(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def xml_values(parent, values):
    for key, value in values.items():
        if isinstance(value, dict):
            xml_values(SubElement(parent, key), value)
        elif isinstance(value, list):
            for entry in value:
                child = SubElement(parent, key)
                if isinstance(entry, dict):
                    xml_values(child, entry)
                else:
                    child.text = scalar(entry)
        elif value is not None:
            parent.set(key, scalar(value))


def reply(values, code=None, message="", payload=None):
    body = {
        "status": "ok" if code is None else "failed",
        "version": "1.16.1",
        "type": "lastbridge",
        "serverVersion": "0.1.0",
        "openSubsonic": True,
    }
    if code is not None:
        body["error"] = {"code": code, "message": message}
    elif payload:
        body.update(payload)
    if values.get("f") == "json":
        return JSONResponse({"subsonic-response": body})
    root = Element("subsonic-response", xmlns="http://subsonic.org/restapi")
    xml_values(root, body)
    return Response(
        tostring(root, encoding="utf-8", xml_declaration=True),
        media_type="application/xml",
    )


def required(values, key):
    if key not in values or not values[key]:
        raise CatalogError(10, f"Required parameter: {key}")
    return values[key]


def integer(values, key, default, maximum=2**31 - 1):
    try:
        value = int(values.get(key, str(default)))
        if not 0 <= value <= maximum:
            raise ValueError
        return value
    except ValueError:
        raise CatalogError(10, f"Invalid parameter: {key}") from None


def folder(values):
    if "musicFolderId" in values and values["musicFolderId"] != "1":
        raise CatalogError(70, "Music folder not found")


def directory_album(album):
    return {k: v for k, v in album.items() if k not in ("name", "song")} | {
        "title": album["name"],
        "isDir": True,
        "parent": album.get("artistId", "1"),
    }


async def dispatch(request, method, values, user):
    catalog = request.app.state.catalog
    folder(values)
    if method == "ping":
        return {}
    if method == "getLicense":
        return {"license": {"valid": True}}
    if method == "getUser":
        name = required(values, "username")
        # Account management lives in the browser. Even browser administrators
        # cannot enumerate another user's protocol profile here.
        if name != user["username"]:
            raise CatalogError(50, "Access denied")
        return {
            "user": {
                "username": name,
                "folder": [1],
                "scrobblingEnabled": False,
                **{
                    role + "Role": False
                    for role in (
                        "admin",
                        "settings",
                        "download",
                        "upload",
                        "playlist",
                        "comment",
                        "podcast",
                        "stream",
                        "jukebox",
                        "share",
                        "video",
                    )
                },
                "coverArtRole": True,
            }
        }
    if method == "getMusicFolders":
        return {
            "musicFolders": {
                "musicFolder": [{"id": 1, "name": "Saved library"}]
            }
        }
    if method == "getScanStatus":
        return {"scanStatus": {"scanning": False, "count": 0}}
    if method in ("getArtists", "getIndexes"):
        return {
            "artists" if method == "getArtists" else "indexes": {
                "ignoredArticles": "",
                "index": [],
                **({"lastModified": 0} if method == "getIndexes" else {}),
            }
        }
    if method in ("getAlbumList", "getAlbumList2"):
        kind = required(values, "type")
        if kind not in (
            "random",
            "newest",
            "highest",
            "frequent",
            "recent",
            "starred",
            "alphabeticalByName",
            "alphabeticalByArtist",
            "byYear",
            "byGenre",
        ):
            raise CatalogError(10, "Invalid album list type")
        integer(values, "size", 10, 500)
        integer(values, "offset", 0)
        if kind == "byYear":
            integer(values, "fromYear", 0, 9999)
            integer(values, "toYear", 9999, 9999)
        if kind == "byGenre":
            required(values, "genre")
        return {
            "albumList2" if method.endswith("2") else "albumList": {
                "album": []
            }
        }
    if method in ("getStarred", "getStarred2"):
        return {
            "starred2" if method.endswith("2") else "starred": {
                "artist": [],
                "album": [],
                "song": [],
            }
        }
    if method == "getGenres":
        return {"genres": {"genre": []}}
    if method == "getPlaylists":
        if values.get("username", user["username"]) != user["username"]:
            raise CatalogError(50, "Access denied")
        return {"playlists": {"playlist": []}}
    if method == "getPlaylist":
        required(values, "id")
        raise CatalogError(70, "Playlist not found")
    if method in ("search2", "search3"):
        if "query" not in values:
            raise CatalogError(10, "Required parameter: query")
        query = values["query"].strip()
        if query == '""':
            query = ""
        if len(query) > 256:
            raise CatalogError(10, "Search query too long")
        pages = {
            kind: (
                integer(values, kind + "Offset", 0),
                min(integer(values, kind + "Count", 20), 100),
            )
            for kind in ("artist", "album", "song")
        }
        kinds = [
            k + "s"
            for k, (offset, count) in pages.items()
            if count and offset < 100
        ]
        # Empty query is the user's synchronizable saved library. Search
        # metadata is public internally but must never populate that library.
        results = (
            await catalog.search(user["id"], query, kinds)
            if (query and kinds)
            else {"artist": [], "album": [], "song": []}
        )
        result = {
            k: results[k][offset: offset + count]
            for k, (offset, count) in pages.items()
        }
        if method == "search2":
            result["album"] = [directory_album(a) for a in result["album"]]
        return {
            "searchResult2" if method == "search2" else "searchResult3": result
        }
    if method in ("getSong", "getAlbum", "getArtist"):
        kind = method[3:].lower()
        return {kind: await catalog.detail(required(values, "id"), kind)}
    if method == "getMusicDirectory":
        item_id = required(values, "id")
        if item_id == "1":
            return {
                "directory": {"id": "1", "name": "Saved library", "child": []}
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
                "child": children,
            }
        }
    if method == "getCoverArt":
        if "size" in values:
            integer(values, "size", 0, 4096)
        data, media_type = await catalog.artwork(
            request.app.state.http_client, required(values, "id")
        )
        # The provider thumbnail is returned unchanged; size is advisory.
        return Response(data, media_type=media_type)
    raise CatalogError(0, "Endpoint not implemented")


@router.api_route("/rest/{method}", methods=["GET", "POST"])
async def endpoint(request: Request, method: str):
    try:
        values = await parameters(request)
    except HTTPException:
        return reply({}, 10, "Invalid request parameters")
    method = method.removesuffix(".view")
    # The specification requires unauthenticated extension discovery. No user
    # state or upstream request is involved in this exception.
    if method == "getOpenSubsonicExtensions":
        return reply(values, payload={"openSubsonicExtensions": EXTENSIONS})
    token_auth = "t" in values or "s" in values
    mechanisms = sum(("p" in values, token_auth, "apiKey" in values))
    if mechanisms > 1 or ("apiKey" in values and "u" in values):
        return reply(values, 43, "Conflicting authentication mechanisms")
    if mechanisms == 0 or ("apiKey" not in values and not values.get("u")):
        return reply(values, 40, "Wrong username or client password")
    user = request.app.state.accounts.client_user(values)
    if user is None:
        code = 44 if "apiKey" in values else 40
        return reply(values, code, "Wrong username or client password")
    if not values.get("v") or not values.get("c"):
        return reply(values, 10, "Required parameters: v and c")
    if user["link_status"] != "linked":
        return reply(
            values, 50, "Link Last.fm in your Lastbridge browser account"
        )
    if values.get("f", "xml") not in ("xml", "json"):
        return reply(values, 0, "Supported formats: XML and JSON")
    try:
        result = await dispatch(request, method, values, user)
        return (
            result
            if isinstance(result, Response)
            else reply(values, payload=result)
        )
    except CatalogError as error:
        return reply(values, error.code, error.message)
