import contextlib
import json
import logging
import sys


def execute(payload):
    from ytmusicapi import YTMusic

    music = YTMusic()
    operation = payload["operation"]
    if operation == "search":
        return {
            kind: music.search(payload["query"], filter=kind, limit=100)[:100]
            for kind in payload["kinds"]
        }
    if operation == "album":
        return music.get_album(payload["source"])
    if operation == "artist":
        return music.get_artist(payload["source"])
    raise ValueError("Unknown operation")


def main():
    logging.disable(logging.CRITICAL)
    payload = json.load(sys.stdin)
    # ytmusicapi does not normally print; reserve stdout for the result even
    # when a future dependency adds progress output.
    # Discard any unexpected dependency output without retaining it in RAM.
    import os

    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
        try:
            result = {"data": execute(payload)}
        except Exception:
            result = {"error": "Metadata provider unavailable"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
