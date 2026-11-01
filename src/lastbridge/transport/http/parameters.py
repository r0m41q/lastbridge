from urllib.parse import parse_qsl

from fastapi import HTTPException, Request


async def parameters(
    request: Request, multiple=frozenset(), *, field_limit=128, body_limit=None
) -> dict:
    pairs = list(request.query_params.multi_items())
    if request.method == "POST":
        if request.headers.get("content-type", "").split(";")[0] != (
            "application/x-www-form-urlencoded"
        ):
            raise HTTPException(415, "Use a URL-encoded form")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > (
                body_limit
                if body_limit is not None
                else (16384 if multiple else 4096)
            ):
                raise HTTPException(413, "Form too large")
        try:
            pairs.extend(
                parse_qsl(
                    body.decode(),
                    keep_blank_values=True,
                    max_num_fields=field_limit if multiple else 32,
                )
            )
        except ValueError, UnicodeError:
            raise HTTPException(400, "Invalid form") from None
    result = {}
    if multiple and len(pairs) > field_limit:
        raise HTTPException(400, "Too many parameters")
    for key, value in pairs:
        if (key in result and key not in multiple) or len(value) > 2048:
            raise HTTPException(400, "Duplicate or oversized parameter")
        if key in multiple:
            result.setdefault(key, []).append(value)
        else:
            result[key] = value
    return result
