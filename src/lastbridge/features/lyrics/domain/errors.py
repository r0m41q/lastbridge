class LyricsUnavailable(Exception):
    """A bounded external lookup failed without exposing request details."""


class CoolingDown(Exception):
    """The external service has asked us to wait."""
