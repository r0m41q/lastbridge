from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi.responses import JSONResponse, Response


def scalar(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def xml_values(parent, values):
    for key, value in values.items():
        if parent.tag in ("line", "lyrics") and key == "value":
            parent.text = value
        elif (
            parent.tag in ("artistInfo", "artistInfo2")
            and key
            in (
                "biography",
                "musicBrainzId",
                "lastFmUrl",
                "smallImageUrl",
                "mediumImageUrl",
                "largeImageUrl",
            )
            and value is not None
        ):
            SubElement(parent, key).text = scalar(value)
        elif isinstance(value, dict):
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


def directory_album(album):
    return {k: v for k, v in album.items() if k not in ("name", "song")} | {
        "title": album["name"],
        "isDir": True,
        "parent": album.get("artistId", "1"),
    }
