import re

RENDITIONS = {"aac": ("m4a", "audio/mp4"), "opus": ("webm", "audio/webm")}
KEY = re.compile(r"[A-Za-z0-9_-]{11}-(?:aac|opus)\Z")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
