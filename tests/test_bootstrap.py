"""Resource ownership survives startup failures and supports explicit fakes."""

import importlib

import pytest
from fastapi.testclient import TestClient
from support.audio import FakeAudio
from support.catalog import FakeProvider

from lastbridge.app import create_app
from lastbridge.bootstrap import Providers
from lastbridge.config import Settings

bootstrap_module = importlib.import_module("lastbridge.bootstrap")


def test_injected_providers_and_explicit_worker_control(tmp_path):
    metadata, audio = FakeProvider(), FakeAudio()
    app = create_app(
        Settings(_env_file=None, data_dir=tmp_path),
        providers=Providers(metadata=metadata, audio=audio),
        start_workers=False,
    )
    with TestClient(app):
        services = app.state.services
        assert services.catalog.provider is metadata
        assert services.audio.provider is audio
        assert services.accounts.database is services.database
        assert services.catalog.repository.database is services.database
        assert services.library.repository.database is services.database
        assert services.library.worker is None
        assert not services.sync.tasks and services.mixes.task is None
        assert not metadata.calls and not audio.calls
    assert services.audio.closed and services.audio.owner.closed
    assert services.http_client.is_closed


@pytest.mark.parametrize("stage", ["library", "mixes"])
def test_partial_startup_closes_acquired_resources(
    tmp_path, monkeypatch, stage
):
    captured = {}
    original_start = bootstrap_module.Library.start

    async def start_library(library, audio, **kwargs):
        captured["library"], captured["audio"] = library, audio
        await original_start(library, audio, **kwargs)
        if stage == "library":
            raise RuntimeError("synthetic startup failure")

    async def start_mixes(mixes):
        captured["client"] = mixes.remote.client
        raise RuntimeError("synthetic startup failure")

    original_sync = bootstrap_module.LastfmSync.start

    async def start_sync(sync):
        captured["sync"] = sync
        await original_sync(sync)

    monkeypatch.setattr(bootstrap_module.Library, "start", start_library)
    monkeypatch.setattr(bootstrap_module.Mixes, "start", start_mixes)
    monkeypatch.setattr(bootstrap_module.LastfmSync, "start", start_sync)
    app = create_app(Settings(_env_file=None, data_dir=tmp_path))
    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        with TestClient(app):
            pass
    assert not app.state.ready
    assert captured["audio"].closed and captured["audio"].owner.closed
    assert captured["library"].worker.done()
    if stage == "mixes":
        assert captured["client"].is_closed
        assert not captured["sync"].tasks

    # Failed startup releases the cache lock for a subsequent process.
    monkeypatch.undo()
    restored = create_app(
        Settings(_env_file=None, data_dir=tmp_path), start_workers=False
    )
    with TestClient(restored):
        assert restored.state.ready
