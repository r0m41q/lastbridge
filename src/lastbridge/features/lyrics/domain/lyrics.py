import re
import unicodedata

STAMP = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:\.(\d{1,3}))?\]")
OFFSET = re.compile(r"\[offset:([+-]?\d{1,6})\]", re.I)
TAG = re.compile(r"\[(?:ar|ti|al|by|re|ve|length):[^\]]*\]", re.I)


def text(value):
    return isinstance(value, str) and all(
        c in "\t\n\r"
        or 0x20 <= ord(c) <= 0xD7FF
        or 0xE000 <= ord(c) <= 0xFFFD
        or 0x10000 <= ord(c) <= 0x10FFFF
        for c in value
    )


def normalize(value):
    # Keep version qualifiers, accents and punctuation. No fuzzy substitution.
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def timed_lines(value, duration):
    if not text(value) or len(value) > 64 * 1024:
        return []
    lines, offset, size = [], 0, 0
    for raw in value.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if match := OFFSET.fullmatch(raw):
            offset = int(match[1])
            if abs(offset) > 60_000:
                return []
            continue
        if TAG.fullmatch(raw):
            continue
        stamps, end = [], 0
        while match := STAMP.match(raw, end):
            fraction = int((match[3] or "0").ljust(3, "0"))
            stamps.append((int(match[1]) * 60 + int(match[2])) * 1000)
            stamps[-1] += fraction
            end = match.end()
        if not stamps or len(raw[end:]) > 2048:
            return []
        size += len(stamps) * len(raw[end:])
        if size > 64 * 1024:
            return []
        lines.extend((start, raw[end:].strip()) for start in stamps)
        if len(lines) > 2000:
            return []
    # Bake the LRC offset into timestamps for consistent client handling.
    if any(start - offset > (duration + 2) * 1000 for start, _ in lines):
        return []
    result = sorted(
        dict.fromkeys((max(0, start - offset), line) for start, line in lines),
        key=lambda row: row[0],
    )
    if not any(line for _, line in result):
        return []
    return [{"start": start, "value": line} for start, line in result]


def structured(song, row):
    if not isinstance(row, dict):
        raise ValueError
    for key, field in (
        ("artist", "artistName"),
        ("title", "trackName"),
        ("album", "albumName"),
    ):
        if not text(row.get(field)) or normalize(song[key]) != normalize(
            row[field]
        ):
            return []
    duration = row.get("duration")
    if (
        type(duration) not in (int, float)
        or not song["duration"] - 2 <= duration <= song["duration"] + 2
    ):
        return []
    if row.get("instrumental") is True:
        return []
    lines = timed_lines(row.get("syncedLyrics"), song["duration"])
    synced = bool(lines)
    if not lines:
        plain = row.get("plainLyrics")
        if text(plain) and len(plain) <= 64 * 1024:
            values = plain.splitlines()
            if len(values) <= 2000 and all(len(v) <= 2048 for v in values):
                lines = [{"value": v} for v in values]
    if not lines or not any(line["value"].strip() for line in lines):
        return []
    return [
        {
            "displayArtist": song["artist"],
            "displayTitle": song["title"],
            "lang": "und",
            "offset": 0,
            "synced": synced,
            "line": lines,
        }
    ]
