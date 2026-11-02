"""Enforce dependency direction across every feature and shared package."""

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "src" / "lastbridge"
FEATURES = (
    "accounts",
    "administration",
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
    feature_module = module.startswith("lastbridge.features.")
    for name in imports(module, tree):
        root = name.split(".")[0]
        if feature_module and name.startswith("lastbridge.features."):
            target = name.split(".")
            if target[2] != feature and target[3:4] != ["domain"]:
                problems.append("cross-feature implementation: " + name)
        if layer == "domain" and root in {
            "os",
            "subprocess",
            "sqlite3",
            "socket",
            "http",
            "asyncio",
            "threading",
            "multiprocessing",
        }:
            problems.append("I/O in domain: " + name)
        if layer == "domain" and name.startswith("urllib.request"):
            problems.append("I/O in domain: " + name)
        if layer == "application" and root in {"sqlite3", "subprocess"}:
            problems.append("concrete I/O in application: " + name)
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
        ("domain", "import sqlite3"),
        ("application", "import subprocess"),
        (
            "adapters.sqlite",
            "from lastbridge.features.accounts.adapters.sqlite "
            "import repository",
        ),
        (
            "adapters.browser",
            "from lastbridge.features.accounts.application import service",
        ),
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


def test_shared_rules_do_not_depend_on_infrastructure():
    for path in (SOURCE / "shared").rglob("*.py"):
        for name in imports(
            "lastbridge.shared.example", ast.parse(path.read_text())
        ):
            assert name.split(".")[0] in sys.stdlib_module_names or (
                name.startswith("lastbridge.shared.")
            ), f"{path}: {name}"


def test_runtime_imports_resolve_without_legacy_feature_facades():
    legacy = {
        "accounts",
        "audio",
        "catalog",
        "library",
        "mixes",
        "radio",
        "sync",
        "lyrics",
        "playlists",
        "recordings",
        "lastfm",
        "ui",
        "web",
        "subsonic",
        "sync_schema",
        "mix_schema",
        "playlist_schema",
        "radio_schema",
        "lyrics_schema",
    }
    for path in SOURCE.rglob("*.py"):
        module = "lastbridge." + ".".join(
            path.relative_to(SOURCE).with_suffix("").parts
        )
        for name in imports(module, ast.parse(path.read_text())):
            parts = name.split(".")
            assert not (
                parts[0] == "lastbridge"
                and len(parts) > 1
                and parts[1] in legacy
            ), f"{path}: obsolete import {name}"


def test_http_uses_only_the_single_service_container():
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Attribute):
                continue
            if isinstance(node.value, ast.Attribute):
                if node.value.attr == "state":
                    assert node.attr in {"services", "config", "ready"}, (
                        f"{path}:{node.lineno}: app.state.{node.attr}"
                    )
