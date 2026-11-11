from urllib.parse import urlsplit

from lastbridge.shared.errors import CatalogError


def station_name(value):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or any(
            ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF or c in "\ufffe\uffff"
            for c in value
        )
    ):
        raise CatalogError(10, "Invalid parameter: name")
    return value.strip()


def station_url(value, field, *, optional=False):
    if optional and value == "":
        return ""
    try:
        if (
            not isinstance(value, str)
            or len(value) > 2048
            or any(c.isspace() or ord(c) < 32 for c in value)
            or any(
                0xD800 <= ord(c) <= 0xDFFF or c in "\ufffe\uffff"
                for c in value
            )
        ):
            raise ValueError
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.port == 0
            or "\\" in value
        ):
            raise ValueError
    except ValueError:
        raise CatalogError(10, f"Invalid parameter: {field}") from None
    return value
