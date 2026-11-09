import re

from lastbridge.providers.youtube_worker import classify_error


def failure_reason(message):
    text = message.casefold()
    if "option not found" in text or "unrecognized option" in text:
        return "ffmpeg_option_missing"
    if "not a suitable output format" in text or (
        "requested output format" in text and "not known" in text
    ):
        return "ffmpeg_muxer_missing"
    if "unknown input format" in text:
        return "ffmpeg_demuxer_missing"
    if "ffmpeg not found" in text or "ffprobe not found" in text:
        return "media_tools_missing"
    if "requested format is not available" in text:
        return "no_audio_format"
    if "signature" in text or "challenge" in text:
        return "player_challenge_failed"
    return classify_error(message)


class DiagnosticLogger:
    """Retain bounded categories, never upstream messages or signed URLs."""

    def __init__(self, diagnostics):
        self.diagnostics = diagnostics

    def debug(self, message):
        # yt-dlp exposes detailed FFmpeg stderr only at debug level.
        if "unrecognized option" in str(message).casefold():
            self.warning(message)

    info = debug

    def warning(self, message):
        reason = failure_reason(str(message))
        if reason != "provider_error":
            self.diagnostics["upstream_reason"] = reason
        match = re.search(r"HTTP Error (\d{3})\b", str(message))
        if match:
            self.diagnostics["http_status"] = int(match[1])

    error = warning


def safe_diagnostics(values):
    """Validate subprocess fields before including them in server logs."""
    if not isinstance(values, dict):
        return {}
    result = {}
    for key in (
        "stage",
        "exception_type",
        "yt_dlp_version",
        "client",
        "format_id",
        "upstream_reason",
    ):
        value = values.get(key)
        if isinstance(value, str) and re.fullmatch(r"[\w.-]{1,64}", value):
            result[key] = value
    for key in ("http_status", "elapsed_ms", "format_count", "returncode"):
        value = values.get(key)
        if type(value) is int and -1_000_000 <= value <= 100_000_000:
            result[key] = value
    return result
