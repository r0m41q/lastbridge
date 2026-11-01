"""Shared fixtures have one registration point, independent of test modules."""

pytest_plugins = (
    "support.accounts",
    "support.catalog",
    "support.audio",
    "support.library",
    "support.sync",
    "support.radio",
    "support.mixes",
    "support.imports",
)
