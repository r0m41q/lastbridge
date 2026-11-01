"""Enforce dependency direction across migrated features."""

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "src" / "lastbridge"
FEATURES = (
    "accounts",
    "catalog",
    "library",
    "playback",
    "playlists",
    "discovery",
    "radio",
    "lastfm_sync",
    "lyrics",
)


def imports(module, tree):
    package = module.rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if node.level:
                name = importlib.util.resolve_name(
                    "." * node.level + name, package
                )
            yield name
            yield from (name + "." + alias.name for alias in node.names)


def violations(module, source):
    tree = ast.parse(source)
    parts = module.split(".")
    problems = []
    feature = parts[2]
    layer = parts[3]
    for name in imports(module, tree):
        root = name.split(".")[0]
        if layer in ("domain", "application"):
            allowed = (
                root in sys.stdlib_module_names
                or name.startswith("lastbridge.shared.")
                or name.startswith(f"lastbridge.features.{feature}.domain")
                or (
                    layer == "application"
                    and (
                        name.startswith(
                            f"lastbridge.features.{feature}.application"
                        )
                        or root == "anyio"
                    )
                )
            )
            if not allowed:
                problems.append(name)
        browser = ".adapters.browser" in module or module.startswith(
            "lastbridge.transport.browser."
        )
        subsonic = ".adapters.subsonic" in module or module.startswith(
            "lastbridge.transport.subsonic."
        )
        if browser and (
            ".adapters.subsonic" in name
            or name.startswith("lastbridge.transport.subsonic.")
            or name == "lastbridge.subsonic"
        ):
            problems.append(name)
        if subsonic and (
            ".adapters.browser" in name
            or name.startswith("lastbridge.transport.browser.")
            or name == "lastbridge.web"
        ):
            problems.append(name)
    if layer in ("domain", "application"):
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.match(
                    r"\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|PRAGMA)\s",
                    node.value,
                ):
                    problems.append("SQL outside SQLite adapter")
    return problems


@pytest.mark.parametrize("feature", FEATURES)
def test_feature_dependency_direction(feature):
    errors = []
    for path in (SOURCE / "features" / feature).rglob("*.py"):
        module = "lastbridge." + ".".join(
            path.relative_to(SOURCE).with_suffix("").parts
        )
        errors.extend(
            f"{module}: {problem}"
            for problem in violations(module, path.read_text())
        )
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize(
    "layer,source",
    [
        ("domain", "from fastapi import Request"),
        ("domain", "from lastbridge.providers.lastfm.client import Lastfm"),
        (
            "application",
            "from lastbridge.features.playlists.application import service",
        ),
        ("application", "from ..adapters.sqlite import repository"),
        ("application", "from lastbridge.accounts import Accounts"),
        (
            "application",
            "def helper():\n"
            "    from lastbridge.features.catalog.application import service",
        ),
        ("application", 'query = "SELECT * FROM users"'),
        (
            "adapters.browser",
            "from ..subsonic import handlers",
        ),
        (
            "adapters.subsonic",
            "from lastbridge.transport.browser.dependencies import COOKIE",
        ),
        (
            "adapters.browser",
            "from lastbridge.transport.subsonic.parameters import required",
        ),
    ],
)
def test_guard_rejects_boundary_violations(layer, source):
    assert violations(f"lastbridge.features.library.{layer}.example", source)


def test_platform_does_not_import_features_or_http():
    for path in (SOURCE / "platform").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for name in imports("lastbridge.platform.example", tree):
            assert not name.startswith(
                ("lastbridge.features.", "fastapi", "starlette")
            )


def test_test_support_has_no_test_module_dependencies():
    tests = SOURCE.parents[1] / "tests"
    for path in tests.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for name in imports("tests.example", tree):
            assert not any(
                part.startswith("test_") for part in name.split(".")
            )


@pytest.mark.parametrize("transport", ["browser", "subsonic"])
def test_transport_adapters_do_not_import_each_other(transport):
    for path in (SOURCE / "transport" / transport).rglob("*.py"):
        module = "lastbridge." + ".".join(
            path.relative_to(SOURCE).with_suffix("").parts
        )
        assert not violations(module, path.read_text())
