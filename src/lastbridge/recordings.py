import hashlib
import json

from lastbridge.providers.youtube import normalize, normalize_title


def recording_key(artist, title):
    values = [normalize(artist), normalize_title(title)]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()
