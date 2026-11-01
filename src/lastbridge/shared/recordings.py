import hashlib
import json
import re
import unicodedata


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"\w+", value))


def normalize_title(value: str) -> str:
    # A trailing performer credit is metadata, not a recording version.
    value = re.sub(
        r"\s*[\[(]\s*(?:feat\.?|ft\.?|featuring)\s+[^)\]]+[)\]]\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return normalize(value)


def recording_key(artist, title):
    values = [normalize(artist), normalize_title(title)]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()
