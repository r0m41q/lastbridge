from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from lastbridge.web import parameters

router = APIRouter()


def reply(
    values: dict[str, str],
    code: int | None = None,
    message: str = "",
) -> Response:
    body = {
        "status": "ok" if code is None else "failed",
        "version": "1.16.1",
        "type": "lastbridge",
        "serverVersion": "0.1.0",
        "openSubsonic": True,
    }
    if code is not None:
        body["error"] = {"code": code, "message": message}
    if values.get("f") == "json":
        return JSONResponse({"subsonic-response": body})
    root = Element(
        "subsonic-response",
        {
            "xmlns": "http://subsonic.org/restapi",
            **{
                key: str(value).lower()
                if isinstance(value, bool)
                else str(value)
                for key, value in body.items()
                if key != "error"
            },
        },
    )
    if code is not None:
        SubElement(root, "error", code=str(code), message=message)
    return Response(
        tostring(root, encoding="utf-8", xml_declaration=True),
        media_type="application/xml",
    )


@router.api_route("/rest/{method}", methods=["GET", "POST"])
async def endpoint(request: Request, method: str):
    try:
        values = await parameters(request)
    except HTTPException:
        return reply({}, 10, "Invalid request parameters")
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
    if method in ("ping", "ping.view"):
        return reply(values)
    return reply(values, 0, "Endpoint not implemented")
