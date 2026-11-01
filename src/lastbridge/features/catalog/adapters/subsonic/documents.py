"""Legacy client representation, including old persisted catalog documents."""


def client_document(kind, document):
    """Make persisted pre-rendition metadata safe for client song parsers."""
    document = dict(document)
    if kind == "song":
        # Feishin unconditionally calls contentType.startsWith(). Unknown media
        # needs a string without inventing an audio format before resolution.
        document.setdefault("contentType", "application/octet-stream")
    child = {"album": "song", "artist": "album"}.get(kind)
    if child and child in document:
        document[child] = [client_document(child, d) for d in document[child]]
    return document
