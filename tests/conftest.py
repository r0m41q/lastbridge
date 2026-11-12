"""Shared fixtures have one registration point, independent of test modules."""

pytest_plugins = (
    "support.accounts",
    "support.catalog",
    "support.artists",
    "support.audio",
    "support.library",
    "support.history",
    "support.sync",
    "support.radio",
    "support.mixes",
    "support.imports",
    "support.corrections",
)
