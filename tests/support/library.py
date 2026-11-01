import pytest

from lastbridge.accounts import Accounts
from lastbridge.audio import AudioCache
from lastbridge.catalog import Catalog
from lastbridge.library import Library
from support.audio import FakeAudio
from support.catalog import SONG


@pytest.fixture
def saved_service(catalog_service):
    client, app, _, _ = catalog_service
    # Explicitly drive the durable worker in most tests, avoiding scheduling
    # races while still exercising real HTTP mutations, SQLite and filesystem.
    app.state.audio.provider = FakeAudio()
    return catalog_service


def run(service, function, *args):
    return service[0].portal.call(function, *args)


def user_id(service, name="admin"):
    return service[1].state.accounts.user(name)["id"]


def complete_jobs(service):
    library = service[1].state.library
    while run(service, library.process_one):
        pass


def make_store(tmp_path):
    accounts = Accounts(tmp_path)
    for name in ("admin", "alice"):
        accounts.create_user(name, "a sufficiently long password")
    catalog = Catalog(accounts)
    song = catalog.song(SONG)
    library = Library(accounts, catalog, "aac")
    audio = AudioCache(
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
