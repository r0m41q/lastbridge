def lyric_file(documents):
    """Serialize normalized lyrics, whose offsets are already applied."""
    if not documents:
        return None, ""
    document = documents[0]
    lines = document["line"]
    if not lines:
        return None, ""
    if document.get("synced"):
        values = []
        for line in lines:
            minutes, remainder = divmod(line["start"], 60000)
            seconds, milliseconds = divmod(remainder, 1000)
            stamp = f"[{minutes:02d}:{seconds:02d}.{milliseconds:03d}]"
            values.append(stamp + line["value"])
        return ".lrc", "\n".join(values) + "\n"
    return ".txt", "\n".join(line["value"] for line in lines) + "\n"
