"""Temporary public path for the shared Last.fm wire adapter."""

from lastbridge.providers.lastfm.client import AUTH_VALUE, Lastfm
from lastbridge.shared.lastfm import LastfmError, LastfmRequestError

__all__ = ["AUTH_VALUE", "Lastfm", "LastfmError", "LastfmRequestError"]
