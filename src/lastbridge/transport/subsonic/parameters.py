from lastbridge.shared.errors import CatalogError


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
