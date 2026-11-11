import re
from urllib.parse import urlsplit

from lastbridge.providers.youtube_worker import classify_error

FAILURE_DETAILS = {
    "bot_confirmation_required",
    "rate_limited",
    "po_token_required",
    "age_restricted",
    "geo_restricted",
    "login_required",
    "private_recording",
    "recording_removed",
    "player_challenge_failed",
    "http_403_unspecified",
}


def failure_detail(message):
    """Describe explicit upstream evidence without guessing a denial cause."""
    text = re.sub(r"https?://\S+", "", message).casefold()
    if "confirm" in text and "not a bot" in text:
        return "bot_confirmation_required"
    if re.search(r"http error 429\b", text) or any(
        term in text
        for term in (
            "too many requests",
            "rate limit",
            "this content isn't available, try again later",
        )
    ):
        return "rate_limited"
    if re.search(r"po[ _-]?token", text) and any(
        term in text for term in ("require", "missing", "not provided")
    ):
        return "po_token_required"
    if any(term in text for term in ("age-restricted", "confirm your age")):
        return "age_restricted"
    if any(
        term in text
        for term in ("not available in your country", "geo-restricted")
    ):
        return "geo_restricted"
    if "private video" in text:
        return "private_recording"
    if "video has been removed" in text:
        return "recording_removed"
    if "sign in" in text or "login required" in text:
        return "login_required"
    if "signature" in text or "challenge" in text:
        return "player_challenge_failed"
    if re.search(r"http error 403\b", text):
        return "http_403_unspecified"
    return None


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
        message = str(message)
        # A 403 before the first progress hook otherwise leaves select_format
        # as the last stage, even though selection already succeeded.
        if "unable to download video data" in message.casefold():
            self.diagnostics["stage"] = "download"
        reason = failure_reason(message)
        if reason != "provider_error":
            self.diagnostics["upstream_reason"] = reason
        match = re.search(r"HTTP Error (\d{3})\b", message)
        if match:
            self.diagnostics["http_status"] = int(match[1])

    def error(self, message):
        self.warning(message)
        detail = failure_detail(str(message))
        previous = self.diagnostics.get("failure_detail")
        if detail and previous in (None, "http_403_unspecified"):
            self.diagnostics["failure_detail"] = detail

    def failure(self, error):
        """Inspect bounded exception links; never read/log a response body."""
        from yt_dlp.networking.exceptions import HTTPError

        pending, seen = [error], set()
        while pending and len(seen) < 8:
            current = pending.pop(0)
            if not isinstance(current, BaseException) or id(current) in seen:
                continue
            seen.add(id(current))
            self.error(str(current))
            if isinstance(current, HTTPError):
                self.diagnostics["http_status"] = current.status
                response = current.response
                try:
                    host = urlsplit(response.url).hostname or ""
                except ValueError:
                    host = ""
                target = "other"
                if host == "googlevideo.com" or host.endswith(
                    ".googlevideo.com"
                ):
                    target = "youtube_media"
                    self.diagnostics["stage"] = "download"
                elif host == "youtube.com" or host.endswith(".youtube.com"):
                    target = "youtube"
                self.diagnostics["request_target"] = target
                retry = response.headers.get("Retry-After", "")
                if re.fullmatch(r"[0-9]{1,6}", retry.strip()):
                    self.diagnostics["retry_after_seconds"] = int(retry)
            pending.extend(
                (
                    current.__cause__,
                    current.__context__,
                    getattr(current, "cause", None),
                )
            )
            info = getattr(current, "exc_info", None)
            if isinstance(info, tuple) and len(info) == 3:
                pending.append(info[1])


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
    detail = values.get("failure_detail")
    if isinstance(detail, str) and detail in FAILURE_DETAILS:
        result["failure_detail"] = detail
    target = values.get("request_target")
    if isinstance(target, str) and target in {
        "youtube_media",
        "youtube",
        "other",
    }:
        result["request_target"] = target
    retry = values.get("retry_after_seconds")
    if type(retry) is int and 0 <= retry <= 999_999:
        result["retry_after_seconds"] = retry
    return result
