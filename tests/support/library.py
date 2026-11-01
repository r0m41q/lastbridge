import pytest

from support.audio import FakeAudio
from support.build_accounts import make_accounts
from support.build_audio import make_audio
from support.build_catalog import make_catalog
from support.build_library import make_library
from support.catalog import SONG


@pytest.fixture
def saved_service(catalog_service):
    client, app, _, _ = catalog_service
    # Explicitly drive the durable worker in most tests, avoiding scheduling
    # races while still exercising real HTTP mutations, SQLite and filesystem.
    app.state.services.audio.provider = FakeAudio()
    return catalog_service


def run(service, function, *args):
    return service[0].portal.call(function, *args)


def user_id(service, name="admin"):
    return service[1].state.services.accounts.user(name)["id"]


def complete_jobs(service):
    library = service[1].state.services.library
    while run(service, library.process_one):
        pass


def make_store(tmp_path):
    accounts = make_accounts(tmp_path)
    for name in ("admin", "alice"):
        accounts.create_user(name, "a sufficiently long password")
    catalog = make_catalog(accounts)
    song = catalog.song(SONG)
    library = make_library(accounts, catalog, "aac")
    audio = make_audio(
        tmp_path / "cache" / "audio",
        saved_directory=tmp_path / "saved" / "audio",
        budget=32768,
        maximum=8192,
        reserve=0,
        provider=FakeAudio(),
        pins=library.pins(),
    )
    library.audio = audio
    return accounts, catalog, library, audio, song
