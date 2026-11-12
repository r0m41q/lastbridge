"""Small presentation helpers shared by recording review screens."""


def wants_json(request):
    return "application/json" in request.headers.get("accept", "")


def status(entry):
    if entry.get("skipped"):
        return "skipped", "Skipped"
    if entry.get("reviewed"):
        return "resolved", "Confirmed"
    if entry.get("song_id"):
        return "resolved", "Matched"
    return "review", (
        "Choose recording" if entry.get("candidates") else "No match"
    )
