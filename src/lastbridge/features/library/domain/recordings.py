def metadata(document):
    return {
        key: document.get(key)
        for key in ("title", "artist", "album", "duration", "isExplicit")
    }


def document_ids(value):
    if isinstance(value, list):
        return set().union(*(document_ids(v) for v in value))
    if isinstance(value, dict):
        return {value.get("id")} | set().union(
            *(document_ids(v) for v in value.values())
        )
    return set()
