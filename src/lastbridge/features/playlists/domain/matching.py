"""Playlist-specific metadata comparison; fuzzy results require review."""

import hashlib
import json
import re
import unicodedata
from difflib import SequenceMatcher

VERSION = 2


def normalize(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(re.findall(r"\w+", value))


def identity(row):
    data = [row.get("videoId"), row.get("title"), row.get("artists")]
    return hashlib.sha256(
        json.dumps(data, sort_keys=True).encode()
    ).hexdigest()


def title_parts(value, artists=()):
    head, separator, suffix = value.partition("|")
    if separator and re.match(
        r"\s*(prod\.?\s|official\s|def jam\b)", suffix, re.I
    ):
        value = head
    value = value.strip()
    for artist in artists:
        value = re.sub(
            r"^" + re.escape(artist) + r"\s*[-–—:]\s*",
            "",
            value,
            flags=re.I,
        )
    # Balanced groups include nested soundtrack descriptions.
    groups, start, depth = [], 0, 0
    for i, char in enumerate(value):
        if char in "([":
            if not depth:
                start = i
            depth += 1
        elif char in ")]" and depth:
            depth -= 1
            if not depth:
                groups.append((start, i + 1, value[start + 1: i]))
    qualifiers, clean = [], None
    for start, end, text in reversed(groups):
        if re.fullmatch(r"\s*clean\s*", text, re.I):
            clean = False
        elif re.match(
            r"\s*(official\b|visualizer\b|from\s+(?:gtavi\b|the "
            r"(?:series|idol)\b)|spider-man\b|"
            r"feat\.?\s|ft\.?\s|featuring\s|prod\b|"
            r"fortnite astronomical event\b)",
            text,
            re.I,
        ):
            pass
        else:
            text = re.sub(r"\s*-?\s*official visualizer", "", text, flags=re.I)
            qualifiers.insert(0, text.strip())
        value = value[:start] + value[end:]
    return value.strip(), tuple(qualifiers), clean


def compare(source, candidate):
    names = [a.get("name", "") for a in source.get("artists", [])]
    credits = [a.get("name", "") for a in candidate.get("artists", [])]
    source_ids = {a.get("id") for a in source.get("artists", [])} - {None}
    target_ids = {a.get("id") for a in candidate.get("artists", [])} - {None}
    artist = bool(source_ids & target_ids) or bool(
        set(map(normalize, names)) & set(map(normalize, credits))
    )
    base, qualifiers, clean = title_parts(source["title"], names)
    other, versions, _ = title_parts(candidate["title"], credits)
    # A credited remixer can be represented only in the catalog title.
    if tuple(map(normalize, qualifiers)) == ("remix",):
        qualified = [q for q in versions if "remix" in q.casefold()]
        if any(
            normalize(name) in normalize(q)
            for name in names
            for q in qualified
        ):
            qualifiers = versions
    version = tuple(map(normalize, qualifiers)) == tuple(
        map(normalize, versions)
    )
    numbers = re.findall(r"\d+", base) == re.findall(r"\d+", other)
    explicit = source.get("isExplicit", clean)
    if clean is False:
        explicit = False
    edition = explicit is None or candidate.get("isExplicit") is explicit
    score = SequenceMatcher(None, normalize(base), normalize(other)).ratio()
    return {
        "artist_ok": artist,
        "version_ok": version and numbers,
        "explicit_ok": edition,
        "score": score,
        "exact": normalize(base) == normalize(other),
    }
